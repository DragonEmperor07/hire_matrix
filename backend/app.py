"""
HireMatrix — Flask backend.

Wires the Python services into HTTP endpoints:
  • Detect Bias       → solution.py logic
  • Shortlist         → reweight.py + catshortlist.py (CatBoost)
  • Process Candidate → cv_extractor → generate_question → transcribe → score → aggregate → interview_report
  • Candidate Review  → fairness.py (BiasAuditor)
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hirematrix")

# ──────────────────────────────────────────────────────────────────────
# APP SETUP
# ──────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")

UPLOAD_DIR = BASE_DIR / "uploads"
REPORT_DIR = BASE_DIR / "reports"
AUDIO_DIR = BASE_DIR / "audio"
RESUME_DIR = BASE_DIR / "resumes"
for d in (UPLOAD_DIR, REPORT_DIR, AUDIO_DIR, RESUME_DIR):
    d.mkdir(exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(FRONTEND_DIR),
    static_folder=str(FRONTEND_DIR / "static"),
)
app.secret_key = os.getenv("SECRET_KEY", "hirematrix-2026")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB


@app.after_request
def add_cors_headers(response):
    origin = os.getenv("FRONTEND_ORIGIN", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

# In-memory per-browser session store — keyed by client-generated session_id.
# Holds the candidate processing pipeline state (CV → questions → answers → scoring).
SESSIONS: Dict[str, Dict] = {}

ALLOWED_SAMPLE_CSVS = {"adult.csv", "attrition.csv"}


# ──────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────

def load_and_clean(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    for col in df.select_dtypes("object").columns:
        df[col] = df[col].str.strip()
    for c in ["fnlwgt", "capital.loss", "capital.gain"]:
        if c in df.columns:
            df = df.drop(columns=[c])
    if "MonthlyIncome" in df.columns and "HighIncome" not in df.columns:
        threshold = df["MonthlyIncome"].median()
        df["HighIncome"] = (df["MonthlyIncome"] > threshold).map(
            {True: "Yes", False: "No"}
        )
    return df


def detect_dataset(columns):
    if "education.num" in columns:
        return "UCI Adult Income", {
            "gender_col": "sex",
            "target_col": "income",
            "positive_val": ">50K",
            "occupation_col": "occupation",
        }
    if "Attrition" in columns:
        return "IBM HR Analytics", {
            "gender_col": "Gender",
            "target_col": "HighIncome",
            "positive_val": "Yes",
            "occupation_col": "JobRole",
        }
    return "Unknown", {}


def _new_session() -> str:
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {
        "cv_data": None,
        "config": None,
        "questions": [],
        "answers": {},      # qid -> {"text": "...", "source": "audio"|"manual"}
        "scoring": None,
        "aggregated": None,
        "report_path": None,
        "candidate_meta": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return sid


def _require_session():
    sid = request.form.get("session_id")
    if not sid and request.is_json:
        sid = (request.get_json(silent=True) or {}).get("session_id")
    if not sid or sid not in SESSIONS:
        return None
    return sid


# ──────────────────────────────────────────────────────────────────────
# BASIC ROUTES
# ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sample/<filename>")
def sample_file(filename):
    """Serve a built-in sample CSV. Used by the frontend's 'Try sample' button."""
    safe = secure_filename(filename)
    if safe not in ALLOWED_SAMPLE_CSVS:
        return jsonify({"error": "sample not available"}), 404
    return send_from_directory(BASE_DIR, safe, as_attachment=True)


@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(REPORT_DIR, filename)


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "active_sessions": len(SESSIONS),
    })


# ══════════════════════════════════════════════════════════════════════
#  DETECT BIAS  (solution.py)
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/upload", methods=["POST"])
def upload_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(f.filename)
    filepath = os.path.join(UPLOAD_DIR, filename)
    f.save(filepath)

    df = load_and_clean(filepath)
    columns = df.columns.tolist()
    dataset_name, suggestions = detect_dataset(columns)
    preview = df.head(10).fillna("").to_dict(orient="records")

    return jsonify({
        "filename": filename,
        "filepath": filepath,
        "columns": columns,
        "dtypes": {c: str(df[c].dtype) for c in columns},
        "rows": len(df),
        "preview": preview,
        "dataset_name": dataset_name,
        "suggestions": suggestions,
    })


@app.route("/api/unique_values", methods=["POST"])
def unique_values():
    data = request.json or {}
    df = load_and_clean(data["filepath"])
    return jsonify({"values": df[data["column"]].dropna().unique().tolist()})


@app.route("/api/bias_analysis", methods=["POST"])
def bias_analysis():
    """solution.py — disparate impact + within-occupation breakdown."""
    data = request.json or {}
    df = load_and_clean(data["filepath"])
    gender_col = data["gender_col"]
    target_col = data["target_col"]
    occupation_col = data["occupation_col"]
    positive_outcome = data["positive_val"]

    audit_df = df[[gender_col, target_col, occupation_col]].copy()
    audit_df[occupation_col] = audit_df[occupation_col].replace("?", "Private/Undisclosed")

    groups = audit_df[gender_col].unique().tolist()
    group_rates = {
        g: round(float((audit_df[audit_df[gender_col] == g][target_col] == positive_outcome).mean()), 4)
        for g in groups
    }
    rates = list(group_rates.values())
    di_score = round(min(rates) / max(rates), 4) if max(rates) > 0 else 0
    parity = round(abs(rates[0] - rates[1]), 4) if len(rates) >= 2 else 0
    di_verdict = "PASSES" if di_score >= 0.8 else ("WARNING" if di_score >= 0.6 else "FAILS")

    biased_jobs, fair_jobs, skipped_jobs = [], [], []
    for occ in audit_df[occupation_col].unique().tolist():
        occ_subset = audit_df[audit_df[occupation_col] == occ]
        occ_rates = {}
        valid = True
        for g in groups:
            grp = occ_subset[occ_subset[gender_col] == g]
            if len(grp) < 5:
                valid = False
                break
            occ_rates[g] = round(float((grp[target_col] == positive_outcome).mean()), 4)
        if not valid:
            skipped_jobs.append(occ)
            continue
        rv = list(occ_rates.values())
        occ_di = round(min(rv) / max(rv), 4) if max(rv) > 0 else 0
        if occ_di < 0.8:
            biased_jobs.append({"name": occ, "di_score": occ_di, "rates": occ_rates})
        else:
            fair_jobs.append({"name": occ, "di_score": occ_di, "rates": occ_rates})

    if di_score < 0.8 and biased_jobs:
        conclusion = "SYSTEMIC GENDER BIAS DETECTED"
        detail = "Bias exists both overall and within specific job roles — pure gender discrimination."
    elif di_score < 0.8:
        conclusion = "OCCUPATIONAL SEGREGATION"
        detail = "Overall bias exists but jobs individually are fair — concentration in lower-paying roles."
    elif biased_jobs:
        conclusion = "HIDDEN BIAS IN SPECIFIC ROLES"
        detail = "Overall numbers look fair but specific jobs show bias — needs targeted investigation."
    else:
        conclusion = "NO SIGNIFICANT BIAS DETECTED"
        detail = "System appears fair both overall and within occupations."

    return jsonify({
        "groups": groups,
        "group_rates": group_rates,
        "di_score": di_score,
        "di_verdict": di_verdict,
        "parity": parity,
        "parity_pass": abs(parity) < 0.1,
        "biased_jobs": sorted(biased_jobs, key=lambda x: x["di_score"]),
        "fair_jobs": sorted(fair_jobs, key=lambda x: x["di_score"]),
        "skipped_jobs": skipped_jobs,
        "conclusion": conclusion,
        "conclusion_detail": detail,
        "total_occupations": len(audit_df[occupation_col].unique()),
    })


# ══════════════════════════════════════════════════════════════════════
#  SHORTLIST  (reweight.py + catshortlist.py / CatBoost)
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/reweight", methods=["POST"])
def reweight():
    """reweight.py — fairness reweighting with auto-skip when DI ≥ 0.8."""
    data = request.json or {}
    df = load_and_clean(data["filepath"])
    gender_col = data["gender_col"]
    income_col = data["target_col"]
    positive_val = data["positive_val"]

    n = len(df)
    p_gender = df[gender_col].value_counts() / n
    p_income = df[income_col].value_counts() / n
    p_joint = df.groupby([gender_col, income_col]).size() / n

    def fw(row):
        joint = p_joint.get((row[gender_col], row[income_col]), 1e-9)
        return (p_gender[row[gender_col]] * p_income[row[income_col]]) / joint

    df["fairness_weight"] = df.apply(fw, axis=1)

    # DI sanity check — does the dataset actually need reweighting?
    rates_by_group = {}
    for g, grp in df.groupby(gender_col):
        rates_by_group[g] = round(float((grp[income_col] == positive_val).mean()), 4)
    rates = list(rates_by_group.values())
    di_check = round(min(rates) / max(rates), 4) if max(rates) > 0 else 0

    needed = di_check < 0.8
    if not needed:
        df["fairness_weight"] = 1.0  # neutralise

    out_path = os.path.join(UPLOAD_DIR, "reweighted_" + os.path.basename(data["filepath"]))
    df.to_csv(out_path, index=False)

    return jsonify({
        "needed": bool(needed),
        "di_before": di_check,
        "group_rates": rates_by_group,
        "weight_stats": {
            "mean": round(float(df["fairness_weight"].mean()), 4),
            "std": round(float(df["fairness_weight"].std()), 4),
            "min": round(float(df["fairness_weight"].min()), 4),
            "max": round(float(df["fairness_weight"].max()), 4),
        },
        "weight_by_group": {
            g: round(float(grp["fairness_weight"].mean()), 4)
            for g, grp in df.groupby(gender_col)
        },
        "output_path": out_path,
        "rows": len(df),
    })


@app.route("/api/detect_proxies", methods=["POST"])
def detect_proxies():
    from sklearn.preprocessing import LabelEncoder

    data = request.json or {}
    df = load_and_clean(data["filepath"])
    gender_col = data["gender_col"]
    threshold = data.get("threshold", 0.3)

    df_enc = df.copy()
    le = LabelEncoder()
    for col in df_enc.columns:
        if df_enc[col].dtype == "object":
            df_enc[col] = le.fit_transform(df_enc[col].astype(str))

    gender_enc = df_enc[gender_col]
    correlations = {}
    for col in df_enc.columns:
        if col == gender_col:
            continue
        c = abs(float(df_enc[col].corr(gender_enc)))
        if not pd.isna(c):
            correlations[col] = round(c, 4)
    correlations = dict(sorted(correlations.items(), key=lambda x: x[1], reverse=True))
    flagged = [c for c, v in correlations.items() if v >= threshold]

    return jsonify({"correlations": correlations, "flagged": flagged, "threshold": threshold})


@app.route("/api/occupations", methods=["POST"])
def list_occupations():
    """Return unique occupation values for the occupation selector in the UI."""
    data = request.json or {}
    df = load_and_clean(data["filepath"])
    col = data.get("occupation_col")
    if not col or col not in df.columns:
        return jsonify({"occupations": []})
    occs = df[col].dropna().unique().tolist()
    counts = {o: int((df[col] == o).sum()) for o in occs}
    return jsonify({
        "occupations": sorted(occs),
        "counts": counts,
    })


@app.route("/api/shortlist", methods=["POST"])
def shortlist():
    """catshortlist.py — CatBoost-based shortlisting with fairness weights and audit."""
    from catboost import CatBoostClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report

    data = request.json or {}
    df = load_and_clean(data["filepath"])
    gender_col = data["gender_col"]
    income_col = data["target_col"]
    positive_val = data["positive_val"]
    occupation_col = data.get("occupation_col", "")
    target_occupation = data.get("target_occupation", "")
    top_n = int(data.get("top_n", 100))
    auto_remove = data.get("auto_remove", [])

    if target_occupation and occupation_col and occupation_col in df.columns:
        df = df[df[occupation_col] == target_occupation].copy()

    if len(df) < 10:
        return jsonify({"error": "Too few candidates after filtering (need ≥ 10)."}), 400

    sample_weights = (
        df["fairness_weight"].values
        if "fairness_weight" in df.columns
        else np.ones(len(df))
    )
    has_weights = "fairness_weight" in df.columns

    drop_set = set(auto_remove + [income_col, "fairness_weight", "merit_score", "final_score"])
    drop_set = [c for c in drop_set if c in df.columns]
    feature_cols = [c for c in df.columns if c not in drop_set]

    X = df[feature_cols].copy()
    y = (df[income_col] == positive_val).astype(int)

    cat_idx = []
    for i, col in enumerate(X.columns):
        if X[col].dtype == object:
            X[col] = X[col].astype(str).fillna("missing")
            cat_idx.append(i)

    X_train, X_test, y_train, y_test, sw_train, _ = train_test_split(
        X, y, sample_weights, test_size=0.3, random_state=42, stratify=y
    )

    model = CatBoostClassifier(
        iterations=300,
        learning_rate=0.05,
        depth=6,
        random_state=42,
        eval_metric="AUC",
        verbose=0,
    )
    model.fit(
        X_train, y_train,
        sample_weight=sw_train,
        cat_features=cat_idx,
        eval_set=(X_test, y_test),
        early_stopping_rounds=30,
    )

    y_pred = model.predict(X_test)
    report = classification_report(y_test, y_pred, output_dict=True)

    # Feature importance (normalised to %)
    imps = model.get_feature_importance()
    total_imp = float(np.sum(imps)) or 1.0
    pairs = sorted(
        ((name, float(imp / total_imp * 100)) for name, imp in zip(feature_cols, imps)),
        key=lambda x: x[1], reverse=True,
    )
    feat_imp = {name: round(pct, 2) for name, pct in pairs}

    # Predict & shortlist
    df = df.copy()
    df["predicted_prob"] = model.predict_proba(X)[:, 1]
    if top_n > len(df):
        top_n = len(df)
    shortlist_df = df.nlargest(top_n, "predicted_prob")

    groups = df[gender_col].unique().tolist()
    shortlist_rates, counts, avg_prob = {}, {}, {}
    for g in groups:
        total = int((df[gender_col] == g).sum())
        sel = int((shortlist_df[gender_col] == g).sum())
        rate = sel / total if total else 0.0
        shortlist_rates[g] = round(rate, 4)
        counts[g] = {"selected": sel, "total": total}
        avg_prob[g] = round(
            float(shortlist_df.loc[shortlist_df[gender_col] == g, "predicted_prob"].mean() * 100),
            2,
        ) if sel else 0.0

    sr = list(shortlist_rates.values())
    di = round(min(sr) / max(sr), 4) if max(sr) > 0 else 0
    parity = round(abs(sr[0] - sr[1]), 4) if len(sr) >= 2 else 0

    # Equal-opportunity (selection rate among qualified)
    eo_rates = {}
    qual_rates = {}
    eo_breakdown = {}
    for g in groups:
        in_g = df[gender_col] == g
        qualified = int(((in_g) & (df[income_col] == positive_val)).sum())
        qualified_selected = int(((shortlist_df[gender_col] == g) & (shortlist_df[income_col] == positive_val)).sum())
        total_in_g = int(in_g.sum())
        eo_rates[g] = round(qualified_selected / qualified, 4) if qualified else 0
        qual_rates[g] = round(qualified / total_in_g, 4) if total_in_g else 0
        eo_breakdown[g] = {
            "qualified": qualified,
            "qualified_selected": qualified_selected,
            "eo_rate": eo_rates[g],
        }
    eo_di = round(min(eo_rates.values()) / max(eo_rates.values()), 4) if max(eo_rates.values()) > 0 else 0.0

    preview_cols = [c for c in [gender_col, income_col, "predicted_prob"] if c in shortlist_df.columns]
    preview = shortlist_df[preview_cols].head(15).round(4).to_dict(orient="records")

    # Persist context for the audit-report endpoint
    audit_ctx = {
        "dataset_name": detect_dataset(df.columns.tolist())[0],
        "gender_col": gender_col,
        "income_col": income_col,
        "target_occupation": target_occupation,
        "is_occ_filtered": bool(target_occupation),
        "total_pool": int(len(df)),
        "top_n": int(top_n),
        "di": di,
        "feature_importance": feat_imp,
        "removed_cols": list(drop_set),
        "fairness_applied": has_weights,
        "eo_di": eo_di,
        "qualification_rates": qual_rates,
        "eo_breakdown": eo_breakdown,
        "gender_breakdown": {
            g: {
                "applied": counts[g]["total"],
                "selected": counts[g]["selected"],
                "rate": shortlist_rates[g],
            }
            for g in groups
        },
    }
    audit_id = uuid.uuid4().hex
    SESSIONS.setdefault("__audits__", {})[audit_id] = audit_ctx

    return jsonify({
        "model": "CatBoost",
        "iterations": model.tree_count_,
        "best_iteration": int(model.get_best_iteration() or 0),
        "train_size": len(X_train),
        "test_size": len(X_test),
        "has_weights": has_weights,
        "accuracy": round(report["accuracy"] * 100, 2),
        "precision": round(report["weighted avg"]["precision"] * 100, 2),
        "recall": round(report["weighted avg"]["recall"] * 100, 2),
        "f1": round(report["weighted avg"]["f1-score"] * 100, 2),
        "feature_importance": feat_imp,
        "features_used": feature_cols,
        "removed_cols": list(drop_set),
        "total_candidates": len(df),
        "shortlisted": top_n,
        "shortlist_rates": shortlist_rates,
        "shortlist_counts": counts,
        "di_score": di,
        "parity": parity,
        "eo_di": eo_di,
        "qualification_rates": qual_rates,
        "eo_breakdown": eo_breakdown,
        "avg_prob": avg_prob,
        "verdict": "FAIR" if di >= 0.8 else "NEEDS REVIEW",
        "preview": preview,
        "audit_id": audit_id,
    })


@app.route("/api/audit_report", methods=["POST"])
def audit_report():
    """Generate the human-friendly HTML audit report for a previous shortlist run."""
    from audit_report import generate_audit_report

    data = request.json or {}
    audit_id = data.get("audit_id")
    audits = SESSIONS.get("__audits__", {})
    if not audit_id or audit_id not in audits:
        return jsonify({"error": "audit_id not found — re-run shortlist first"}), 404
    ctx = audits[audit_id]

    out_name = f"audit_{audit_id}.html"
    out_path = os.path.join(REPORT_DIR, out_name)
    generate_audit_report(
        output_path=out_path,
        dataset_name=ctx["dataset_name"],
        model_name="CatBoost Classifier",
        target_occupation=ctx["target_occupation"],
        is_occ_filtered=ctx["is_occ_filtered"],
        total_pool=ctx["total_pool"],
        gender_col=ctx["gender_col"],
        gender_breakdown=ctx["gender_breakdown"],
        top_n=ctx["top_n"],
        di=ctx["di"],
        feature_importance=ctx["feature_importance"],
        removed_cols=ctx["removed_cols"],
        fairness_applied=ctx["fairness_applied"],
        eo_di=ctx["eo_di"],
        qualification_rates=ctx["qualification_rates"],
        eo_breakdown=ctx["eo_breakdown"],
    )
    return jsonify({"url": f"/reports/{out_name}", "filename": out_name})


# ══════════════════════════════════════════════════════════════════════
#  PROCESS CANDIDATE  (CV → Questions → Audio → Score → Report)
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/cv_extract", methods=["POST"])
def cv_extract():
    """Stage 1 — parse uploaded resume PDF into structured CV data."""
    from cv_extractor import CVExtractor, pdf_to_text

    if "file" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF resumes are supported"}), 400

    sid = request.form.get("session_id") or _new_session()
    if sid not in SESSIONS:
        SESSIONS[sid] = SESSIONS.get(sid) or {}

    filename = secure_filename(f.filename)
    path = os.path.join(RESUME_DIR, f"{sid}_{filename}")
    f.save(path)

    try:
        text = pdf_to_text(path)
        cv_data = CVExtractor(text).extract_all()
    except Exception as e:
        logger.exception("CV extraction failed")
        return jsonify({"error": f"CV extraction failed: {e}"}), 500

    SESSIONS[sid] = SESSIONS.get(sid) or {}
    SESSIONS[sid]["cv_data"] = cv_data
    SESSIONS[sid]["resume_path"] = path
    SESSIONS[sid].setdefault("answers", {})

    contact = cv_data.get("contact_info", {})
    SESSIONS[sid]["candidate_meta"] = {
        "name": contact.get("name") or "Candidate",
        "email": contact.get("email"),
    }

    return jsonify({
        "session_id": sid,
        "cv_data": cv_data,
        "summary": {
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "location": contact.get("location"),
            "skills": cv_data.get("skills", {}).get("all_skills", []),
            "skill_count": cv_data.get("skills", {}).get("count", 0),
            "experience_years": cv_data.get("experience_years"),
            "experience_count": len(cv_data.get("experience_detailed", []) or cv_data.get("experience", []) or []),
            "education_count": len(cv_data.get("education", []) or []),
            "project_count": len(cv_data.get("projects", []) or []),
        }
    })


@app.route("/api/generate_questions", methods=["POST"])
def generate_questions():
    """Stage 2 — call generate_question.QuestionGenerator on the parsed CV."""
    data = request.json or {}
    sid = data.get("session_id")
    if not sid or sid not in SESSIONS or not SESSIONS[sid].get("cv_data"):
        return jsonify({"error": "No active session — upload a resume first"}), 400

    config = {
        "job_role": data.get("job_role", "Software Engineer"),
        "company_type": data.get("company_type", "a mid-size tech company that values technical depth and collaboration"),
        "difficulty": data.get("difficulty", "mid level - practical experience and code quality"),
        "interview_style": data.get("interview_style", "balanced mix of technical depth and behavioral questions"),
        "num_questions": int(data.get("num_questions", 5)),
        "custom_instructions": data.get("custom_instructions", ""),
    }

    SESSIONS[sid]["config"] = config
    SESSIONS[sid]["candidate_meta"]["role"] = config["job_role"]

    from generate_question import QuestionGenerator
    used_fallback = False
    fallback_reason = None
    try:
        generator = QuestionGenerator()
    except ValueError:
        # No GEMINI_API_KEY configured — fall through to the class's own fallback
        generator = None
        used_fallback = True
        fallback_reason = "GEMINI_API_KEY is not set on the server"

    try:
        if generator is not None:
            questions = generator.generate_questions(SESSIONS[sid]["cv_data"], config)
        else:
            questions = QuestionGenerator.__dict__["_generate_fallback_questions"](
                QuestionGenerator.__new__(QuestionGenerator), config
            )
    except Exception as e:
        # Any uncaught Gemini error (invalid key, deprecated model, quota,
        # network) lands here. Return JSON with the reason and the fallback
        # questions so the UI can keep moving instead of throwing
        # "Non-JSON response (500)".
        logger.exception("generate_questions failed — returning fallback")
        used_fallback = True
        fallback_reason = f"{type(e).__name__}: {e}"
        questions = QuestionGenerator.__dict__["_generate_fallback_questions"](
            QuestionGenerator.__new__(QuestionGenerator), config
        )

    SESSIONS[sid]["questions"] = questions
    SESSIONS[sid]["answers"] = {}  # reset answers when questions change

    return jsonify({
        "session_id": sid,
        "questions": questions,
        "count": len(questions),
        "config": config,
        "used_fallback": used_fallback,
        "fallback_reason": fallback_reason,
    })


@app.route("/api/submit_answer", methods=["POST"])
def submit_answer():
    """
    Stage 3 — store the candidate's answer for a single question.
    Accepts either:
      - multipart with `file` (audio) → transcribed via Whisper if available
      - form/json with `text` (manual transcript fallback when Whisper isn't installed)
    """
    sid = request.form.get("session_id") or (request.json or {}).get("session_id")
    qid = request.form.get("qid") or (request.json or {}).get("qid")
    if not sid or sid not in SESSIONS:
        return jsonify({"error": "Invalid session_id"}), 400
    if not qid:
        return jsonify({"error": "Missing qid"}), 400

    SESSIONS[sid].setdefault("answers", {})

    # Audio path
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        path = os.path.join(AUDIO_DIR, f"{sid}_{secure_filename(qid)}_{secure_filename(f.filename)}")
        f.save(path)
        try:
            from transcriptions_pro import AudioTranscriber
            transcriber = AudioTranscriber(model_size="base")
            result = transcriber.transcribe(path)
            text = result.get("text", "")
            error = result.get("error")
            duration = result.get("duration", 0.0)
            source = "audio"
        except ImportError as e:
            return jsonify({
                "error": (
                    "Audio transcription requires the openai-whisper package and ffmpeg. "
                    "Either install them or paste the answer text directly."
                ),
                "detail": str(e),
            }), 501
        except Exception as e:
            logger.exception("Transcription failed")
            return jsonify({"error": f"Transcription failed: {e}"}), 500

        SESSIONS[sid]["answers"][qid] = {
            "text": text, "duration": duration, "error": error,
            "source": source, "audio_path": path,
        }
        return jsonify({"qid": qid, "text": text, "duration": duration, "source": source, "error": error})

    # Manual-text fallback
    text = (request.form.get("text") or (request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Provide either an audio file or text"}), 400
    SESSIONS[sid]["answers"][qid] = {
        "text": text, "duration": 0.0, "error": None,
        "source": "manual", "audio_path": None,
    }
    return jsonify({"qid": qid, "text": text, "source": "manual"})


@app.route("/api/score_session", methods=["POST"])
def score_session():
    """Stage 4 — score every question + aggregate to a candidate verdict."""
    data = request.json or {}
    sid = data.get("session_id")
    if not sid or sid not in SESSIONS:
        return jsonify({"error": "Invalid session_id"}), 400

    sess = SESSIONS[sid]
    questions = sess.get("questions") or []
    answers = sess.get("answers") or {}
    if not questions:
        return jsonify({"error": "Generate questions first"}), 400

    # Build {qid: transcript-dict} matching score.py's expected shape
    transcripts = {}
    for i in range(len(questions)):
        qid = f"q{i+1}"
        ans = answers.get(qid)
        if not ans:
            transcripts[qid] = {"text": "", "duration": 0.0, "segments": [], "error": None}
        else:
            transcripts[qid] = {
                "text": ans.get("text", ""),
                "duration": ans.get("duration", 0.0),
                "segments": [],
                "error": ans.get("error"),
            }

    from score import AnswerScorer
    try:
        scorer = AnswerScorer()
        scoring = scorer.score_session(questions, transcripts)
    except ValueError:
        # No GEMINI_API_KEY — score every answer with the class's own fallbacks
        per_q = []
        for i, q in enumerate(questions):
            qid = f"q{i+1}"
            txt = transcripts[qid].get("text", "")
            if not txt:
                base = AnswerScorer.__dict__["_no_answer_score"](AnswerScorer.__new__(AnswerScorer))
            else:
                base = AnswerScorer.__dict__["_fallback_score"]()
            base["question_id"] = qid
            base["question"] = q.get("question", "")
            base["skill_tested"] = q.get("skill_tested", "")
            per_q.append(base)
        scoring = {
            "per_question": per_q,
            "overall": {
                "average": round(sum(r.get("aggregate", 0) for r in per_q) / max(1, len(per_q)), 2),
                "scored_count": len(per_q),
                "total": len(per_q),
                "red_flags": [],
            }
        }

    from aggregrate import aggregate_candidate_scores
    aggregated = aggregate_candidate_scores(scoring.get("per_question", []), questions=questions)

    sess["scoring"] = scoring
    sess["aggregated"] = aggregated

    return jsonify({
        "session_id": sid,
        "scoring": scoring,
        "aggregated": aggregated,
    })


@app.route("/api/interview_report", methods=["POST"])
def interview_report():
    """Stage 5 — render the candidate interview HTML report."""
    from interview_report import generate_interview_report

    data = request.json or {}
    sid = data.get("session_id")
    if not sid or sid not in SESSIONS:
        return jsonify({"error": "Invalid session_id"}), 400
    sess = SESSIONS[sid]
    if not sess.get("aggregated"):
        return jsonify({"error": "Score the session first"}), 400

    full_report = {
        "candidate_id": sid,
        "metadata": sess.get("candidate_meta", {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stages_failed": [],
        "cv_data": sess.get("cv_data", {}),
        "questions": sess.get("questions", []),
        "transcripts": sess.get("answers", {}),
        "scoring": sess.get("scoring", {}),
        "aggregated_scores": sess.get("aggregated", {}),
    }

    out_name = f"interview_{sid}.html"
    out_path = os.path.join(REPORT_DIR, out_name)
    generate_interview_report(out_path, full_report, candidate_metadata=sess.get("candidate_meta", {}))

    sess["report_path"] = out_path

    # Also write the JSON for cohort-level fairness audits later
    json_name = f"interview_{sid}.json"
    json_path = os.path.join(REPORT_DIR, json_name)
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump(full_report, fp, indent=2, ensure_ascii=False, default=str)

    return jsonify({
        "url": f"/reports/{out_name}",
        "filename": out_name,
        "json_url": f"/reports/{json_name}",
    })


# ══════════════════════════════════════════════════════════════════════
#  CANDIDATE REVIEW  (fairness.py — cohort bias audit + dashboard)
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/review_cohort", methods=["POST"])
def review_cohort():
    """
    Accepts multiple uploaded JSON candidate reports (the json_url output above)
    OR a CSV with columns: candidate_id, gender, recommendation, overall_score.
    Returns dashboard data + (optional) fairness-audit results.
    """
    run_fairness = request.form.get("run_fairness", "false").lower() == "true"
    protected_attr = request.form.get("protected_attr", "gender")
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Upload at least one candidate report (JSON or CSV)"}), 400

    candidates = []
    for f in files:
        if not f.filename:
            continue
        name = secure_filename(f.filename)
        path = os.path.join(UPLOAD_DIR, f"cohort_{uuid.uuid4().hex[:8]}_{name}")
        f.save(path)
        try:
            if name.lower().endswith(".csv"):
                df = pd.read_csv(path)
                for _, row in df.iterrows():
                    candidates.append({
                        "candidate_id": str(row.get("candidate_id") or row.get("id") or ""),
                        "name": row.get("name") or row.get("candidate_id") or "Candidate",
                        "gender": row.get("gender") or row.get("Gender") or "unknown",
                        "overall_score": float(row.get("overall_score") or row.get("score") or 0.0),
                        "technical_score": float(row.get("technical_score") or 0.0),
                        "communication_score": float(row.get("communication_score") or 0.0),
                        "recommendation": str(row.get("recommendation") or "").lower() or "maybe",
                        "ai_risk": str(row.get("ai_risk") or "low"),
                    })
            elif name.lower().endswith(".json"):
                with open(path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                agg = data.get("aggregated_scores") or data.get("aggregated") or {}
                meta = data.get("metadata") or {}
                cand = {
                    "candidate_id": data.get("candidate_id") or name,
                    "name": meta.get("name") or data.get("candidate_id") or name,
                    "gender": meta.get("gender") or "unknown",
                    "overall_score": float(agg.get("overall_score") or 0.0),
                    "technical_score": float(agg.get("technical_score") or 0.0),
                    "communication_score": float(agg.get("communication_score") or 0.0),
                    "recommendation": str(agg.get("recommendation") or "maybe").lower(),
                    "ai_risk": str(agg.get("ai_risk") or "low"),
                    "red_flags": agg.get("red_flags") or [],
                }
                candidates.append(cand)
        except Exception as e:
            logger.exception("Failed parsing cohort file %s", name)
            return jsonify({"error": f"Could not parse {name}: {e}"}), 400

    if not candidates:
        return jsonify({"error": "No usable candidate records were parsed from the uploads"}), 400

    # Sort: best to worst
    candidates.sort(key=lambda c: c.get("overall_score", 0.0), reverse=True)

    # Dashboard summary
    rec_counts = {}
    for c in candidates:
        rec_counts[c["recommendation"]] = rec_counts.get(c["recommendation"], 0) + 1
    avg_overall = sum(c["overall_score"] for c in candidates) / len(candidates)
    avg_tech = sum(c["technical_score"] for c in candidates) / len(candidates)
    avg_comm = sum(c["communication_score"] for c in candidates) / len(candidates)

    dashboard = {
        "candidates": candidates,
        "total": len(candidates),
        "rec_counts": rec_counts,
        "avg_overall": round(avg_overall, 2),
        "avg_technical": round(avg_tech, 2),
        "avg_communication": round(avg_comm, 2),
    }

    fairness = None
    if run_fairness:
        from fairness import BiasAuditor
        df = pd.DataFrame(candidates)
        if protected_attr not in df.columns:
            fairness = {"error": f"Protected attribute '{protected_attr}' not in records"}
        else:
            fairness = BiasAuditor().audit_cohort(df, protected_attr)

    return jsonify({"dashboard": dashboard, "fairness": fairness})


# ──────────────────────────────────────────────────────────────────────
# ENTRY
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Note: do not enable Flask's debug auto-reloader here — on Windows the
    # watchdog reloader picks up changes inside site-packages and enters an
    # infinite restart loop, which makes the frontend's fetch() calls fail
    # intermittently ("failed to fetch"). Run the server cleanly instead.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)
