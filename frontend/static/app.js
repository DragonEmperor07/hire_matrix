/* ────────────────────────────────────────────────────────────────
   HireMatrix — frontend controller
   ──────────────────────────────────────────────────────────────── */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const API_BASE = (window.HIREMATRIX_API_BASE || "").replace(/\/$/, "");

const STATE = {
  // Detect-bias / Shortlist share dataset metadata once a CSV is uploaded
  bias:      { filepath: null, columns: [] },
  shortlist: { filepath: null, columns: [], reweighted: null, occupations: [] },
  // Process-candidate session
  process:   { sessionId: null, cv: null, questions: [], answers: {}, scoring: null, aggregated: null, reportUrl: null, jsonUrl: null },
  // Cohort review
  cohort:    { files: [] },
};

/* ─────────────────────────────── TOASTS ─────────────────────────────── */

function toast(message, kind = "info", ttl = 3500) {
  const root = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateX(40px)";
    setTimeout(() => el.remove(), 280);
  }, ttl);
}

/* ─────────────────────────────── HTTP HELPERS ─────────────────────────────── */

function apiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path}`;
}

async function api(url, opts = {}) {
  const res = await fetch(apiUrl(url), opts);
  let body;
  try { body = await res.json(); }
  catch { body = { error: `Non-JSON response (${res.status})` }; }
  if (!res.ok) {
    const err = body.error || `Request failed (${res.status})`;
    throw new Error(err);
  }
  return body;
}

const apiJSON  = (url, payload) => api(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
const apiForm  = (url, formData) => api(url, { method: "POST", body: formData });

/* ─────────────────────────────── NAME POPUP ─────────────────────────────── */

function setupNamePopup() {
  const overlay = $("#nameOverlay");
  const input   = $("#nameInput");
  const submit  = $("#nameSubmit");

  const saved = localStorage.getItem("hm_user_name");
  if (saved && saved.trim()) {
    applyName(saved);
    overlay.style.display = "none";
  } else {
    input.focus();
  }

  function go() {
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    localStorage.setItem("hm_user_name", name);
    applyName(name);
    overlay.style.opacity = "0";
    setTimeout(() => overlay.style.display = "none", 260);
  }
  submit.addEventListener("click", go);
  input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

function applyName(name) {
  $("#welcomeName").textContent = name.toUpperCase();
  $("#userName").textContent = name;
  $("#userAvatar").textContent = name.trim()[0].toUpperCase();
  $("#userChip").style.display = "flex";
}

/* ─────────────────────────────── NAV / SCROLL ─────────────────────────────── */

function setupNav() {
  // Click-to-jump tabs inside the services group
  $$(".services-tabs button").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.jump;
      const el = document.getElementById(target);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      $$(".services-tabs button").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  // Highlight which top-nav item the user is on (via IntersectionObserver)
  const sections = ["home", "about", "services"]
    .map(id => document.getElementById(id))
    .filter(Boolean);

  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          const id = e.target.id;
          $$(".nav a[data-nav]").forEach(a => a.classList.toggle("active", a.dataset.nav === id));
        }
      });
    }, { rootMargin: "-40% 0px -55% 0px" });
    sections.forEach(s => io.observe(s));
  }

  // Highlight services tab as user scrolls past each service-card
  if ("IntersectionObserver" in window) {
    const cardIO = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          $$(".services-tabs button").forEach(b => {
            b.classList.toggle("active", b.dataset.jump === e.target.id);
          });
        }
      });
    }, { rootMargin: "-30% 0px -65% 0px" });
    ["detect-bias", "shortlist", "process", "review"].forEach(id => {
      const el = document.getElementById(id);
      if (el) cardIO.observe(el);
    });
  }
}

/* ─────────────────────────────── DROPZONE HELPER ─────────────────────────────── */

function bindDropzone(zoneSel, inputSel, onFiles, opts = { multiple: false }) {
  const zone = typeof zoneSel === "string" ? $(zoneSel) : zoneSel;
  const input = typeof inputSel === "string" ? $(inputSel) : inputSel;
  if (!zone || !input) return;

  zone.addEventListener("click", () => input.click());
  ["dragenter", "dragover"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("dragover"); }));
  zone.addEventListener("drop", e => {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      onFiles(opts.multiple ? Array.from(e.dataTransfer.files) : e.dataTransfer.files[0]);
    }
  });
  input.addEventListener("change", () => {
    if (input.files.length) {
      onFiles(opts.multiple ? Array.from(input.files) : input.files[0]);
    }
  });
}

/* ──────────────────────────────────────────────────────────────────
   COMMON: upload CSV + populate column selectors
   ────────────────────────────────────────────────────────────────── */

async function uploadCSV(file) {
  const fd = new FormData();
  fd.append("file", file);
  return apiForm("/api/upload", fd);
}

function fillSelect(selectEl, options, selected) {
  selectEl.innerHTML = options.map(o => `<option value="${o}">${o}</option>`).join("");
  if (selected && options.includes(selected)) selectEl.value = selected;
}

/* ──────────────────────────────────────────────────────────────────
   SERVICE 1 — DETECT BIAS  (solution.py)
   ────────────────────────────────────────────────────────────────── */

function setupDetectBias() {
  bindDropzone("#biasUpload", "#biasFile", handleBiasFile);
  $("#biasReset").addEventListener("click", () => {
    $("#biasFile").value = "";
    $("#biasFormWrap").style.display = "none";
    $("#biasResults").style.display = "none";
    STATE.bias = { filepath: null, columns: [] };
  });
  $("#biasRun").addEventListener("click", runBiasAudit);

  $$('button[data-sample][data-target="bias"]').forEach(btn => {
    btn.addEventListener("click", async () => {
      try {
        const res = await fetch(apiUrl(`/sample/${btn.dataset.sample}`));
        const blob = await res.blob();
        const file = new File([blob], btn.dataset.sample, { type: "text/csv" });
        await handleBiasFile(file);
      } catch (e) { toast(`Could not load sample: ${e.message}`, "error"); }
    });
  });
}

async function handleBiasFile(file) {
  toast(`Uploading ${file.name}…`, "info", 1800);
  try {
    const res = await uploadCSV(file);
    STATE.bias.filepath = res.filepath;
    STATE.bias.columns = res.columns;
    fillSelect($("#biasGender"), res.columns, res.suggestions.gender_col);
    fillSelect($("#biasTarget"), res.columns, res.suggestions.target_col);
    fillSelect($("#biasOccupation"), res.columns, res.suggestions.occupation_col);
    $("#biasPositive").value = res.suggestions.positive_val || "";
    $("#biasFormWrap").style.display = "block";
    toast(`Loaded ${res.dataset_name} — ${res.rows.toLocaleString()} rows`, "success");
  } catch (e) { toast(`Upload failed: ${e.message}`, "error"); }
}

async function runBiasAudit() {
  const btn = $("#biasRun");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Analyzing…';
  try {
    const r = await apiJSON("/api/bias_analysis", {
      filepath: STATE.bias.filepath,
      gender_col: $("#biasGender").value,
      target_col: $("#biasTarget").value,
      positive_val: $("#biasPositive").value,
      occupation_col: $("#biasOccupation").value,
    });
    renderBiasResults(r);
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "Run Bias Audit"; }
}

function renderBiasResults(r) {
  const wrap = $("#biasResults");
  wrap.style.display = "block";

  const verdictKind = r.di_verdict === "PASSES" ? "pass" : (r.di_verdict === "WARNING" ? "warn" : "fail");

  const groupBars = Object.entries(r.group_rates).map(([g, rate]) => `
    <div class="bar-row">
      <div class="name">${g}</div>
      <div class="bar"><div style="width:${rate * 100}%"></div></div>
      <div class="pct">${(rate * 100).toFixed(1)}%</div>
    </div>`).join("");

  const biasedHtml = r.biased_jobs.length === 0
    ? `<p style="color:var(--muted);">None — no occupation flagged below 0.8.</p>`
    : r.biased_jobs.slice(0, 8).map(j => `
      <div class="bar-row">
        <div class="name">${j.name}</div>
        <div class="bar"><div style="width:${j.di_score * 100}%; background:linear-gradient(90deg, #ef4444, #f87171);"></div></div>
        <div class="pct">${j.di_score}</div>
      </div>`).join("");

  const fairHtml = r.fair_jobs.length === 0
    ? `<p style="color:var(--muted);">None.</p>`
    : r.fair_jobs.slice(0, 8).map(j => `
      <div class="bar-row">
        <div class="name">${j.name}</div>
        <div class="bar"><div style="width:${j.di_score * 100}%; background:linear-gradient(90deg, #16a34a, #4ade80);"></div></div>
        <div class="pct">${j.di_score}</div>
      </div>`).join("");

  wrap.innerHTML = `
    <div class="verdict-banner ${verdictKind}">
      <div>
        <div class="label">${r.conclusion}</div>
        <div class="text">${r.conclusion_detail}</div>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><div class="label">Disparate Impact</div><div class="value">${r.di_score}</div><div class="sub">Threshold ≥ 0.80 (EEOC)</div></div>
      <div class="stat-card"><div class="label">Statistical Parity</div><div class="value">${r.parity}</div><div class="sub">${r.parity_pass ? "Passes (<0.10)" : "Fails (≥0.10)"}</div></div>
      <div class="stat-card"><div class="label">Verdict</div><div class="value">${r.di_verdict}</div></div>
      <div class="stat-card"><div class="label">Occupations Audited</div><div class="value">${r.total_occupations}</div></div>
    </div>

    <div class="result-block">
      <h4>Positive-outcome rate by group</h4>
      ${groupBars}
    </div>

    <div class="result-block">
      <h4>Biased Occupations (DI &lt; 0.8)</h4>
      ${biasedHtml}
    </div>

    <div class="result-block">
      <h4>Fair Occupations (DI ≥ 0.8) — top 8</h4>
      ${fairHtml}
    </div>
  `;
  wrap.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ──────────────────────────────────────────────────────────────────
   SERVICE 2 — SHORTLIST  (reweight.py + catshortlist.py)
   ────────────────────────────────────────────────────────────────── */

function setupShortlist() {
  bindDropzone("#shortlistUpload", "#shortlistFile", handleShortlistFile);
  $("#slCheckReweight").addEventListener("click", runReweight);
  $("#slRun").addEventListener("click", runCatBoost);

  $$('button[data-sample][data-target="shortlist"]').forEach(btn => {
    btn.addEventListener("click", async () => {
      try {
        const res = await fetch(apiUrl(`/sample/${btn.dataset.sample}`));
        const blob = await res.blob();
        const file = new File([blob], btn.dataset.sample, { type: "text/csv" });
        await handleShortlistFile(file);
      } catch (e) { toast(`Could not load sample: ${e.message}`, "error"); }
    });
  });
}

async function handleShortlistFile(file) {
  toast(`Uploading ${file.name}…`, "info", 1800);
  try {
    const res = await uploadCSV(file);
    STATE.shortlist.filepath = res.filepath;
    STATE.shortlist.columns = res.columns;
    fillSelect($("#slGender"), res.columns, res.suggestions.gender_col);
    fillSelect($("#slTarget"), res.columns, res.suggestions.target_col);
    fillSelect($("#slOccupation"), res.columns, res.suggestions.occupation_col);
    $("#slPositive").value = res.suggestions.positive_val || "";
    $("#shortlistFormWrap").style.display = "block";
    $("#slReweightResult").style.display = "none";
    $("#slShortlistForm").style.display = "none";
    $("#slResults").style.display = "none";
    toast(`Loaded ${res.dataset_name} — ${res.rows.toLocaleString()} rows`, "success");
  } catch (e) { toast(`Upload failed: ${e.message}`, "error"); }
}

async function runReweight() {
  const btn = $("#slCheckReweight");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Checking…';
  try {
    const r = await apiJSON("/api/reweight", {
      filepath: STATE.shortlist.filepath,
      gender_col: $("#slGender").value,
      target_col: $("#slTarget").value,
      positive_val: $("#slPositive").value,
    });
    STATE.shortlist.reweighted = r.output_path;

    const groupBars = Object.entries(r.group_rates).map(([g, rate]) => `
      <div class="bar-row">
        <div class="name">${g}</div>
        <div class="bar"><div style="width:${rate * 100}%"></div></div>
        <div class="pct">${(rate * 100).toFixed(1)}%</div>
      </div>`).join("");

    const verdict = r.needed
      ? `<div class="verdict-banner warn"><div><div class="label">REWEIGHTING APPLIED</div><div class="text">DI was ${r.di_before} (below 0.80). Fairness weights have been computed and will be passed to CatBoost.</div></div></div>`
      : `<div class="verdict-banner pass"><div><div class="label">DATA ALREADY FAIR</div><div class="text">DI was ${r.di_before} (≥0.80). Reweighting set to neutral (1.0). CatBoost will train on the raw distribution.</div></div></div>`;

    $("#slReweightResult").style.display = "block";
    $("#slReweightResult").innerHTML = `
      ${verdict}
      <div class="result-block">
        <h4>Positive-outcome rate by gender</h4>
        ${groupBars}
      </div>
      <div class="stat-grid">
        <div class="stat-card"><div class="label">Mean weight</div><div class="value">${r.weight_stats.mean}</div></div>
        <div class="stat-card"><div class="label">Std</div><div class="value">${r.weight_stats.std}</div></div>
        <div class="stat-card"><div class="label">Min</div><div class="value">${r.weight_stats.min}</div></div>
        <div class="stat-card"><div class="label">Max</div><div class="value">${r.weight_stats.max}</div></div>
      </div>
    `;

    // Now load occupation list for the next step
    const occRes = await apiJSON("/api/occupations", {
      filepath: r.output_path,
      occupation_col: $("#slOccupation").value,
    });
    STATE.shortlist.occupations = occRes.occupations;
    fillSelect($("#slTargetOcc"),
      occRes.occupations.map(o => `${o} (${occRes.counts[o] || 0})`));
    // Replace the values back to the bare occupation names
    Array.from($("#slTargetOcc").options).forEach((opt, i) => {
      opt.value = occRes.occupations[i];
    });
    $("#slShortlistForm").style.display = "block";
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "1. Check if Reweighting Needed"; }
}

async function runCatBoost() {
  const btn = $("#slRun");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Training CatBoost…';
  try {
    const r = await apiJSON("/api/shortlist", {
      filepath: STATE.shortlist.reweighted,
      gender_col: $("#slGender").value,
      target_col: $("#slTarget").value,
      positive_val: $("#slPositive").value,
      occupation_col: $("#slOccupation").value,
      target_occupation: $("#slTargetOcc").value,
      top_n: parseInt($("#slTopN").value || "50", 10),
    });
    renderShortlistResults(r);
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "2. Run CatBoost Shortlist"; }
}

function renderShortlistResults(r) {
  const wrap = $("#slResults");
  wrap.style.display = "block";

  const verdictKind = r.verdict === "FAIR" ? "pass" : "warn";
  const featTop = Object.entries(r.feature_importance).slice(0, 8);
  const maxImp = featTop.length ? featTop[0][1] : 1;
  const featBars = featTop.map(([f, v]) => `
    <div class="bar-row">
      <div class="name">${f}</div>
      <div class="bar"><div style="width:${(v / maxImp) * 100}%"></div></div>
      <div class="pct">${v}%</div>
    </div>`).join("");

  const rateBars = Object.entries(r.shortlist_rates).map(([g, v]) => `
    <div class="bar-row">
      <div class="name">${g} <small style="color:var(--muted);">(${r.shortlist_counts[g].selected}/${r.shortlist_counts[g].total})</small></div>
      <div class="bar"><div style="width:${v * 100}%"></div></div>
      <div class="pct">${(v * 100).toFixed(1)}%</div>
    </div>`).join("");

  // Top candidates preview
  const cols = r.preview.length ? Object.keys(r.preview[0]) : [];
  const previewTbl = r.preview.length === 0 ? "" : `
    <div class="result-block">
      <h4>Top candidates (preview)</h4>
      <div class="preview-table-wrap">
        <table class="preview-table">
          <thead><tr>${cols.map(c => `<th>${c}</th>`).join("")}</tr></thead>
          <tbody>
            ${r.preview.map(row => `<tr>${cols.map(c => `<td>${row[c] ?? ""}</td>`).join("")}</tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>`;

  wrap.innerHTML = `
    <div class="verdict-banner ${verdictKind}">
      <div>
        <div class="label">${r.verdict}</div>
        <div class="text">DI ${r.di_score} · Equal-opportunity DI ${r.eo_di} · Parity ${r.parity}</div>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><div class="label">Pool</div><div class="value">${r.total_candidates}</div><div class="sub">candidates</div></div>
      <div class="stat-card"><div class="label">Shortlisted</div><div class="value">${r.shortlisted}</div></div>
      <div class="stat-card"><div class="label">Accuracy</div><div class="value">${r.accuracy}%</div></div>
      <div class="stat-card"><div class="label">F1</div><div class="value">${r.f1}%</div></div>
    </div>

    <div class="result-block">
      <h4>Selection rate by gender</h4>
      ${rateBars}
    </div>

    <div class="result-block">
      <h4>Top features used by CatBoost</h4>
      ${featBars}
    </div>

    ${previewTbl}

    <div class="row-actions">
      <button class="btn ghost" id="slDownloadAudit">View Audit Report</button>
    </div>
  `;
  wrap.scrollIntoView({ behavior: "smooth", block: "start" });

  $("#slDownloadAudit").addEventListener("click", async () => {
    try {
      const a = await apiJSON("/api/audit_report", { audit_id: r.audit_id });
      window.open(a.url, "_blank");
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ──────────────────────────────────────────────────────────────────
   SERVICE 3 — PROCESS CANDIDATES (5-step wizard)
   ────────────────────────────────────────────────────────────────── */

function setupProcess() {
  bindDropzone("#cvUpload", "#cvFile", handleCV);
  $("#cvNext").addEventListener("click", () => goStage(2));

  $("#qGenerate").addEventListener("click", generateQuestions);
  $("#qNext").addEventListener("click", () => { renderAnswerStage(); goStage(3); });

  $("#aNext").addEventListener("click", () => goStage(4));

  $("#scoreRun").addEventListener("click", runScoring);
  $("#sNext").addEventListener("click", async () => {
    goStage(5);
    await buildReport();
  });

  $("#reportView").addEventListener("click", () => {
    if (STATE.process.reportUrl) window.open(STATE.process.reportUrl, "_blank");
  });
  $("#reportDownload").addEventListener("click", () => {
    if (STATE.process.reportUrl) {
      const a = document.createElement("a");
      a.href = STATE.process.reportUrl;
      a.download = STATE.process.reportUrl.split("/").pop();
      a.click();
    }
  });
  $("#newCandidate").addEventListener("click", resetProcess);

  $$('[data-back]').forEach(b => b.addEventListener("click", () => goStage(parseInt(b.dataset.back, 10))));
}

function goStage(n) {
  $$(".wizard-stage").forEach(s => s.classList.toggle("active", parseInt(s.dataset.stage, 10) === n));
  $$("#wizardSteps .step").forEach(s => {
    const sn = parseInt(s.dataset.step, 10);
    s.classList.toggle("active", sn === n);
    s.classList.toggle("done", sn < n);
  });
  document.getElementById("process").scrollIntoView({ behavior: "smooth", block: "start" });
}

function resetProcess() {
  STATE.process = { sessionId: null, cv: null, questions: [], answers: {}, scoring: null, aggregated: null, reportUrl: null, jsonUrl: null };
  $("#cvFile").value = "";
  $("#cvSummary").style.display = "none";
  $("#cvNext").disabled = true;
  $("#qList").innerHTML = "";
  $("#qNext").disabled = true;
  $("#answerList").innerHTML = "";
  $("#scoreResults").style.display = "none";
  $("#sNext").disabled = true;
  $("#reportPreview").innerHTML = "";
  $("#reportView").disabled = true;
  $("#reportDownload").disabled = true;
  goStage(1);
}

async function handleCV(file) {
  toast("Parsing resume…", "info", 1800);
  const fd = new FormData();
  fd.append("file", file);
  if (STATE.process.sessionId) fd.append("session_id", STATE.process.sessionId);

  try {
    const r = await apiForm("/api/cv_extract", fd);
    STATE.process.sessionId = r.session_id;
    STATE.process.cv = r.cv_data;

    const s = r.summary;
    const skills = s.skills.slice(0, 12).map(sk => `<span class="tag accent">${sk}</span>`).join("");
    $("#cvSummary").style.display = "block";
    $("#cvSummary").innerHTML = `
      <div class="result-block">
        <h4>Extracted profile</h4>
        <div class="stat-grid">
          <div class="stat-card"><div class="label">Name</div><div class="value">${s.name || "—"}</div></div>
          <div class="stat-card"><div class="label">Skills</div><div class="value">${s.skill_count}</div></div>
          <div class="stat-card"><div class="label">Experience entries</div><div class="value">${s.experience_count}</div></div>
          <div class="stat-card"><div class="label">Projects</div><div class="value">${s.project_count}</div></div>
        </div>
        <div style="margin-top:14px;">
          <div style="font-size:13px; color:var(--muted); margin-bottom:8px;">
            ${s.email || "no email"} · ${s.phone || "no phone"} · ${s.location || "no location"}
          </div>
          <div>${skills || '<span style="color:var(--muted);">No skills detected</span>'}</div>
        </div>
      </div>`;
    $("#cvNext").disabled = false;
    toast("Resume parsed", "success");
  } catch (e) { toast(e.message, "error"); }
}

async function generateQuestions() {
  const btn = $("#qGenerate");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Generating…';
  try {
    const r = await apiJSON("/api/generate_questions", {
      session_id: STATE.process.sessionId,
      job_role: $("#qJobRole").value || "Software Engineer",
      difficulty: $("#qDifficulty").value,
      interview_style: $("#qStyle").value,
      num_questions: parseInt($("#qNum").value || "5", 10),
    });
    STATE.process.questions = r.questions;
    renderQuestions(r.questions);
    $("#qNext").disabled = false;
    toast(`Generated ${r.count} questions`, "success");
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "Generate Questions"; }
}

function renderQuestions(qs) {
  $("#qList").innerHTML = qs.map((q, i) => `
    <div class="q-card">
      <div class="qn">Question ${i + 1} · ${q.skill_tested || ""}</div>
      <div class="qtxt">${q.question || ""}</div>
      <div class="meta">
        <span class="tag accent">${q.type || "n/a"}</span>
        <span class="tag">${(q.difficulty || "—").toUpperCase()}</span>
        <span class="tag">${q.category || ""}</span>
      </div>
      ${q.expected_keywords?.length ? `<div style="font-size:12px; color:var(--muted);"><strong>Keywords:</strong> ${q.expected_keywords.join(", ")}</div>` : ""}
    </div>`).join("");
}

function renderAnswerStage() {
  const list = $("#answerList");
  list.innerHTML = STATE.process.questions.map((q, i) => {
    const qid = `q${i + 1}`;
    const stored = STATE.process.answers[qid];
    const status = stored
      ? `<span class="answer-status ok">✓ ${stored.source === "audio" ? `Transcribed (${stored.duration?.toFixed(1)}s)` : "Manual answer saved"}</span>`
      : `<span class="answer-status">No answer yet</span>`;
    return `
      <div class="q-card" data-qid="${qid}">
        <div class="qn">Question ${i + 1}</div>
        <div class="qtxt">${q.question || ""}</div>
        <textarea placeholder="Type or paste the candidate's answer here…" id="ans-${qid}">${stored?.text || ""}</textarea>
        <div class="answer-controls">
          <input type="file" accept="audio/*,video/mp4,.webm,.m4a,.ogg,.flac" id="audio-${qid}" />
          <button class="btn subtle sm" data-upload-audio="${qid}">Upload Audio</button>
          <button class="btn ghost sm" data-save-text="${qid}">Save Typed Answer</button>
          ${status}
        </div>
      </div>`;
  }).join("");

  list.querySelectorAll('[data-upload-audio]').forEach(b => {
    b.addEventListener("click", () => {
      const qid = b.dataset.uploadAudio;
      $(`#audio-${qid}`).click();
    });
  });
  list.querySelectorAll('input[type=file][id^="audio-"]').forEach(inp => {
    inp.addEventListener("change", () => {
      const qid = inp.id.replace("audio-", "");
      if (inp.files[0]) submitAudio(qid, inp.files[0]);
    });
  });
  list.querySelectorAll('[data-save-text]').forEach(b => {
    b.addEventListener("click", () => {
      const qid = b.dataset.saveText;
      submitText(qid, $(`#ans-${qid}`).value);
    });
  });
}

async function submitAudio(qid, file) {
  toast(`Transcribing answer for ${qid}…`, "info", 1500);
  const fd = new FormData();
  fd.append("session_id", STATE.process.sessionId);
  fd.append("qid", qid);
  fd.append("file", file);
  try {
    const r = await apiForm("/api/submit_answer", fd);
    STATE.process.answers[qid] = { text: r.text, duration: r.duration, source: "audio" };
    renderAnswerStage();
    toast(`Saved transcript for ${qid}`, "success");
  } catch (e) { toast(`${qid}: ${e.message}`, "error"); }
}

async function submitText(qid, text) {
  if (!text.trim()) { toast("Type something first", "error"); return; }
  const fd = new FormData();
  fd.append("session_id", STATE.process.sessionId);
  fd.append("qid", qid);
  fd.append("text", text);
  try {
    const r = await apiForm("/api/submit_answer", fd);
    STATE.process.answers[qid] = { text: r.text, source: "manual" };
    renderAnswerStage();
    toast(`Saved typed answer for ${qid}`, "success");
  } catch (e) { toast(`${qid}: ${e.message}`, "error"); }
}

async function runScoring() {
  const btn = $("#scoreRun");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Scoring with Gemini…';
  try {
    const r = await apiJSON("/api/score_session", { session_id: STATE.process.sessionId });
    STATE.process.scoring = r.scoring;
    STATE.process.aggregated = r.aggregated;
    renderScoreResults(r.aggregated);
    $("#sNext").disabled = false;
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "Run Scoring Pipeline"; }
}

function renderScoreResults(agg) {
  const wrap = $("#scoreResults");
  wrap.style.display = "block";
  const recColor = agg.recommendation === "strong_hire" || agg.recommendation === "hire" ? "pass"
                  : agg.recommendation === "maybe" ? "warn" : "fail";

  const dimRows = Object.entries(agg.score_breakdown || {}).map(([dim, v]) => `
    <div class="bar-row">
      <div class="name">${dim.replace(/_/g, " ")}</div>
      <div class="bar"><div style="width:${v * 10}%"></div></div>
      <div class="pct">${v.toFixed(1)}</div>
    </div>`).join("");

  wrap.innerHTML = `
    <div class="verdict-banner ${recColor}">
      <div>
        <div class="label">${(agg.recommendation || "").toUpperCase().replace("_", " ")}</div>
        <div class="text">${agg.recommendation_reason || ""}</div>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><div class="label">Overall</div><div class="value">${agg.overall_score}</div><div class="sub">/ 10</div></div>
      <div class="stat-card"><div class="label">Technical</div><div class="value">${agg.technical_score}</div><div class="sub">/ 10</div></div>
      <div class="stat-card"><div class="label">Communication</div><div class="value">${agg.communication_score}</div><div class="sub">/ 10</div></div>
      <div class="stat-card"><div class="label">AI Risk</div><div class="value">${(agg.ai_risk || "").toUpperCase()}</div><div class="sub">${agg.high_ai_count} flagged</div></div>
    </div>

    <div class="result-block">
      <h4>Score breakdown</h4>
      ${dimRows}
    </div>
  `;
}

async function buildReport() {
  $("#reportPreview").innerHTML = `<div style="text-align:center; padding:24px;"><span class="spinner"></span> Building report…</div>`;
  try {
    const r = await apiJSON("/api/interview_report", { session_id: STATE.process.sessionId });
    STATE.process.reportUrl = apiUrl(r.url);
    STATE.process.jsonUrl = apiUrl(r.json_url);
    $("#reportView").disabled = false;
    $("#reportDownload").disabled = false;
    $("#reportPreview").innerHTML = `
      <div class="result-block">
        <h4>Report ready</h4>
        <p>HTML report has been generated. Use the buttons on the bottom-right to view or download. The recruiter-friendly file is print-ready (Cmd/Ctrl + P from the browser to save as PDF).</p>
        <p style="margin-top:8px; font-size:13px; color:var(--muted);">JSON for cohort review: <a href="${STATE.process.jsonUrl}" target="_blank">${STATE.process.jsonUrl.split("/").pop()}</a></p>
      </div>
      <iframe src="${STATE.process.reportUrl}" style="width:100%; height:600px; margin-top:14px; border:1px solid var(--border); border-radius:var(--radius-md); background:white;"></iframe>
    `;
  } catch (e) { toast(e.message, "error"); $("#reportPreview").innerHTML = ""; }
}

/* ──────────────────────────────────────────────────────────────────
   SERVICE 4 — CANDIDATE REVIEW (cohort)
   ────────────────────────────────────────────────────────────────── */

function setupReview() {
  bindDropzone("#cohortUpload", "#cohortFiles", handleCohortFiles, { multiple: true });
  $("#cohortReset").addEventListener("click", () => {
    STATE.cohort.files = [];
    $("#cohortFiles").value = "";
    $("#cohortFileList").innerHTML = "";
    $("#cohortResults").style.display = "none";
  });
  $("#cohortRun").addEventListener("click", runCohortReview);
}

function handleCohortFiles(files) {
  STATE.cohort.files = files;
  $("#cohortFileList").innerHTML = `
    <div class="result-block" style="margin-top:0;">
      <h4>${files.length} file${files.length !== 1 ? "s" : ""} ready</h4>
      <div>${files.map(f => `<span class="tag accent">${f.name}</span>`).join("")}</div>
    </div>`;
}

async function runCohortReview() {
  if (!STATE.cohort.files.length) { toast("Upload at least one report first", "error"); return; }
  const btn = $("#cohortRun");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Building dashboard…';
  try {
    const fd = new FormData();
    STATE.cohort.files.forEach(f => fd.append("files", f));
    fd.append("run_fairness", $("#fairnessToggle").checked ? "true" : "false");
    fd.append("protected_attr", $("#protectedAttr").value || "gender");
    const r = await apiForm("/api/review_cohort", fd);
    renderCohortDashboard(r);
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "Generate Dashboard"; }
}

let cohortCharts = [];
function destroyCohortCharts() {
  cohortCharts.forEach(c => { try { c.destroy(); } catch {} });
  cohortCharts = [];
}

function renderCohortDashboard(r) {
  destroyCohortCharts();
  const d = r.dashboard;
  const wrap = $("#cohortResults");
  wrap.style.display = "block";

  const recCounts = d.rec_counts || {};
  const candidatesHtml = d.candidates.map((c, i) => `
    <div class="candidate-row">
      <div class="rank">#${i + 1}</div>
      <div class="name">${c.name}<small>${c.candidate_id}${c.gender && c.gender !== "unknown" ? " · " + c.gender : ""}</small></div>
      <div class="num"><div class="lbl">Overall</div>${c.overall_score?.toFixed?.(1) ?? c.overall_score ?? "—"}</div>
      <div class="num opt"><div class="lbl">Technical</div>${c.technical_score?.toFixed?.(1) ?? "—"}</div>
      <div class="num opt"><div class="lbl">Comm.</div>${c.communication_score?.toFixed?.(1) ?? "—"}</div>
      <div><span class="tag ${recPillClass(c.recommendation)}">${(c.recommendation || "").replace("_", " ")}</span></div>
    </div>`).join("");

  let fairnessBlock = "";
  if (r.fairness && !r.fairness.error) {
    const f = r.fairness;
    const verdict = f.overall_pass ? "pass" : "fail";
    const groupBars = Object.entries(f.selection_rates || {}).map(([g, v]) => `
      <div class="bar-row">
        <div class="name">${g}</div>
        <div class="bar"><div style="width:${v * 100}%"></div></div>
        <div class="pct">${(v * 100).toFixed(1)}%</div>
      </div>`).join("");
    const recsHtml = (f.recommendations || []).map(rec => `<li>${rec}</li>`).join("");

    fairnessBlock = `
      <div class="verdict-banner ${verdict}">
        <div>
          <div class="label">Fairness audit — DI ${f.disparate_impact_ratio}</div>
          <div class="text">${f.passes_disparate_impact ? "Passes the four-fifths rule (≥ 0.80)" : "Fails the four-fifths rule (< 0.80)"} · Statistical parity diff ${f.statistical_parity_diff}</div>
        </div>
      </div>
      <div class="result-block">
        <h4>Selection rate by ${f.protected_attribute}</h4>
        ${groupBars}
        ${f.low_confidence ? `<p style="margin-top:10px; color:var(--warning); font-size:13px;">⚠ Low confidence: undersized groups ${(f.undersized_groups || []).join(", ")}</p>` : ""}
      </div>
      <div class="result-block">
        <h4>Recommendations</h4>
        <ul style="margin:0; padding-left:20px;">${recsHtml}</ul>
      </div>`;
  } else if (r.fairness && r.fairness.error) {
    fairnessBlock = `<div class="verdict-banner warn"><div><div class="label">FAIRNESS UNAVAILABLE</div><div class="text">${r.fairness.error}</div></div></div>`;
  }

  wrap.innerHTML = `
    <div class="dash-summary">
      <div class="card"><div class="label">Candidates</div><div class="value">${d.total}</div></div>
      <div class="card"><div class="label">Avg Overall</div><div class="value">${d.avg_overall}</div><div class="sub">/ 10</div></div>
      <div class="card"><div class="label">Avg Technical</div><div class="value">${d.avg_technical}</div><div class="sub">/ 10</div></div>
      <div class="card"><div class="label">Avg Communication</div><div class="value">${d.avg_communication}</div><div class="sub">/ 10</div></div>
    </div>

    <div class="chart-wrap">
      <h4>Recommendation distribution</h4>
      <canvas id="recChart" height="120"></canvas>
    </div>

    <div class="chart-wrap">
      <h4>Overall scores</h4>
      <canvas id="scoreChart" height="160"></canvas>
    </div>

    <div class="result-block">
      <h4>Ranked candidates (best → worst)</h4>
      ${candidatesHtml}
    </div>

    ${fairnessBlock}
  `;

  // Charts
  const recCtx = $("#recChart").getContext("2d");
  cohortCharts.push(new Chart(recCtx, {
    type: "doughnut",
    data: {
      labels: Object.keys(recCounts).map(k => k.replace("_", " ")),
      datasets: [{
        data: Object.values(recCounts),
        backgroundColor: ["#16a34a", "#ff8a3d", "#fbbf24", "#dc2626"],
        borderWidth: 0,
      }],
    },
    options: {
      plugins: { legend: { position: "right" } },
      cutout: "65%",
    },
  }));

  const scoreCtx = $("#scoreChart").getContext("2d");
  cohortCharts.push(new Chart(scoreCtx, {
    type: "bar",
    data: {
      labels: d.candidates.map(c => c.name),
      datasets: [
        { label: "Overall", data: d.candidates.map(c => c.overall_score), backgroundColor: "#ff8a3d" },
        { label: "Technical", data: d.candidates.map(c => c.technical_score), backgroundColor: "#ffae6b" },
        { label: "Communication", data: d.candidates.map(c => c.communication_score), backgroundColor: "#ffd2a6" },
      ],
    },
    options: {
      responsive: true,
      scales: { y: { beginAtZero: true, max: 10 } },
    },
  }));

  wrap.scrollIntoView({ behavior: "smooth", block: "start" });
}

function recPillClass(rec) {
  if (rec === "strong_hire" || rec === "hire") return "success";
  if (rec === "maybe") return "warn";
  return "danger";
}

/* ─────────────────────────────── BOOT ─────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  setupNamePopup();
  setupNav();
  setupDetectBias();
  setupShortlist();
  setupProcess();
  setupReview();
});
