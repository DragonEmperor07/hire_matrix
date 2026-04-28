import pandas as pd

# STEP 1 — LOAD DATA
path = input("Enter the path to your CSV file: ")
df = pd.read_csv(path)

# Strip whitespace
df.columns = df.columns.str.strip()
for col in df.select_dtypes('object').columns:
    df[col] = df[col].str.strip()

# Drop UCI specific columns if present
cols_to_drop = ['fnlwgt', 'capital.loss', 'capital.gain']
cols_to_drop = [c for c in cols_to_drop if c in df.columns]
df = df.drop(columns=cols_to_drop)

# Show columns before asking user
print("\nColumns in your dataset:")
print(df.columns.tolist())

target_col_input = input("\nEnter target column name (e.g. 'income'): ")

# Auto convert continuous target to binary if numeric
if df[target_col_input].dtype in ['int64', 'float64']:
    threshold = df[target_col_input].median()
    binary_col = target_col_input + '_binary'
    df[binary_col] = (df[target_col_input] > threshold).map({True: 'Yes', False: 'No'})
    print(f"\n✅ '{target_col_input}' is numeric — auto converted to binary")
    print(f"   Threshold (median): {threshold:,.0f}")
    print(f"   Yes (above median): {(df[binary_col]=='Yes').sum()}")
    print(f"   No  (below median): {(df[binary_col]=='No').sum()}")
    target_col_input = binary_col

# STEP 2 — ASK WHICH COLUMNS
gender_col = input("\nEnter gender column name: ")
target_col = target_col_input
occupation_col = input("Enter occupation column name (e.g. 'occupation'): ")

# STEP 3 — SEPARATE DATA
audit_df = df[[gender_col, target_col, occupation_col]].copy()
audit_df[occupation_col] = audit_df[occupation_col].replace('?', 'Private/Undisclosed')

print("\nAudit dataframe:")
print(audit_df.head())

# STEP 4 — OVERALL BIAS CHECK
print("\n" + "="*50)
print("OVERALL BIAS ANALYSIS")
print("="*50)

groups = audit_df[gender_col].unique()
print(f"Gender groups found: {groups}")
print(f"Unique values in '{target_col}': {audit_df[target_col].unique()}")

positive_outcome = input(f"\nWhich value is the positive outcome? (e.g. '>50K'): ")

group_rates = {}
for group in groups:
    subset = audit_df[audit_df[gender_col] == group]
    rate = (subset[target_col] == positive_outcome).mean()
    group_rates[group] = round(rate, 4)
    print(f"{group}: {round(rate * 100, 2)}% positive outcome rate")

rates = list(group_rates.values())
di_score = round(min(rates) / max(rates), 4) if max(rates) > 0 else 0
print(f"\nDisparate Impact Score: {di_score}")

if di_score >= 0.8:
    print("✅ PASSES — Fair overall outcome")
elif di_score >= 0.6:
    print("⚠️ WARNING — Borderline overall bias")
else:
    print("❌ FAILS — Severe overall bias")

# STEP 5 — FAIRNESS MATRIX
print("\n" + "="*50)
print("FAIRNESS MATRIX")
print("="*50)

# Disparate Impact (already calculated)
print(f"1. Disparate Impact Score:        {di_score}")
print(f"   Threshold: 0.8 | Legal standard: EEOC, EU AI Act")

# Statistical Parity
parity = round(group_rates[groups[0]] - group_rates[groups[1]], 4)
print(f"\n2. Statistical Parity Difference: {abs(parity)}")
print(f"   ({groups[0]}: {group_rates[groups[0]]} | {groups[1]}: {group_rates[groups[1]]})")
print(f"   Threshold: <0.1 | Closer to 0 = fairer")

if abs(parity) < 0.1:
    print("   ✅ PASSES Statistical Parity")
else:
    print("   ❌ FAILS Statistical Parity")

# STEP 6 — WITHIN OCCUPATION BIAS CHECK
print("\n" + "="*50)
print("WITHIN-OCCUPATION BIAS ANALYSIS")
print("(Isolates pure gender bias from job type differences)")
print("="*50)

occupations = audit_df[occupation_col].unique()
biased_jobs = []
fair_jobs = []
skipped_jobs = []

for occ in occupations:
    occ_subset = audit_df[audit_df[occupation_col] == occ]
    occ_rates = {}
    valid = True

    for group in groups:
        grp_subset = occ_subset[occ_subset[gender_col] == group]
        if len(grp_subset) < 5:
            valid = False
            break
        rate = (grp_subset[target_col] == positive_outcome).mean()
        occ_rates[group] = round(rate, 4)

    if not valid:
        skipped_jobs.append(occ)
        continue

    occ_rate_values = list(occ_rates.values())
    occ_di = round(min(occ_rate_values) / max(occ_rate_values), 4) if max(occ_rate_values) > 0 else 0

    if occ_di < 0.8:
        biased_jobs.append((occ, occ_di))
    else:
        fair_jobs.append((occ, occ_di))

print(f"\n✅ Fair occupations ({len(fair_jobs)}):")
for job, score in sorted(fair_jobs, key=lambda x: x[1]):
    print(f"   {job}: DI Score = {score}")

print(f"\n❌ Biased occupations ({len(biased_jobs)}):")
for job, score in sorted(biased_jobs, key=lambda x: x[1]):
    print(f"   {job}: DI Score = {score}")

print(f"\n⚠️ Skipped (insufficient data): {len(skipped_jobs)} occupations")

# STEP 7 — FINAL VERDICT
print("\n" + "="*50)
print("FINAL VERDICT")
print("="*50)
print(f"Disparate Impact Score:     {di_score}")
print(f"Statistical Parity Diff:    {abs(parity)}")
print(f"Biased occupations:         {len(biased_jobs)}")
print(f"Fair occupations:           {len(fair_jobs)}")

if di_score < 0.8 and len(biased_jobs) > 0:
    print("\n🚨 CONCLUSION: SYSTEMIC GENDER BIAS DETECTED")
    print("   Bias exists both overall AND within specific job roles")
    print("   This is pure gender discrimination, not just occupational difference")
elif di_score < 0.8 and len(biased_jobs) == 0:
    print("\n⚠️ CONCLUSION: OCCUPATIONAL SEGREGATION")
    print("   Overall bias exists BUT jobs individually are fair")
    print("   Women may be concentrated in lower-paying job categories")
elif di_score >= 0.8 and len(biased_jobs) > 0:
    print("\n⚠️ CONCLUSION: HIDDEN BIAS IN SPECIFIC ROLES")
    print("   Overall numbers look fair BUT specific jobs show bias")
    print("   Needs targeted investigation")
else:
    print("\n✅ CONCLUSION: NO SIGNIFICANT BIAS DETECTED")
    print("   System appears fair both overall and within occupations")