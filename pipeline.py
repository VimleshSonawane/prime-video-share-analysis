"""
Prime Video share-of-TV analysis: repeatable pipeline.

Run:  python pipeline.py
In:   raw/gauge_tracker.csv          (Nielsen Gauge figures, one row per month x metric)
      raw/prime_video_events.csv     (live sports + launch timeline)
      raw/netflix_engagement_2026H1.xlsx  (Netflix "What We Watched", Jan-Jun 2026)
Out:  output/*.csv  (Tableau-ready)  +  output/data_quality_report.txt

Three stages, so a new data drop can be re-run end to end:
  1. QUALITY  - check inputs before trusting them
  2. CLEAN    - standardize, flag, enrich
  3. OUTPUT   - write the tables the dashboard reads
"""
from pathlib import Path
import pandas as pd

RAW, OUT = Path("raw"), Path("output")
OUT.mkdir(exist_ok=True)
report = []          # every check appends a line; saved at the end
def log(msg): report.append(msg); print(msg)

# Netflix's published headline for H1 2026 ("more than 97B hours").
# Used to reconcile the file before analysis.
NETFLIX_PUBLISHED_TOTAL_HOURS = 97e9

# ---------------------------------------------------------------- 1. QUALITY
log("=== STAGE 1: QUALITY CHECKS ===")

gauge = pd.read_csv(RAW / "gauge_tracker.csv", dtype={"month": str})
events = pd.read_csv(RAW / "prime_video_events.csv", dtype={"month": str})

# 1a. Duplicates: one value per month x metric, or the trend lines double-count.
dups = gauge.duplicated(["month", "metric"]).sum()
log(f"Gauge rows: {len(gauge)} | duplicate month x metric: {dups}")

# 1b. Category shares can't exceed 100% of TV time in a month.
cats = gauge[gauge.metric.isin(["Streaming", "Cable", "Broadcast"])]
over = cats.groupby("month").share_pct.sum().loc[lambda s: s > 100]
log(f"Months where streaming+cable+broadcast > 100%: {len(over)}")

# 1c. Source mix: how much rests on Nielsen's own releases vs trade press.
tier = gauge.source_tier.value_counts(normalize=True).mul(100).round(0)
log(f"Source tiers (%): {tier.to_dict()}")

# 1d. Methodology breaks are flagged in notes; count them so they're visible.
breaks = gauge.notes.fillna("").str.contains("RECLASSIFICATION|METHODOLOGY|Restated")
log(f"Rows flagged for methodology breaks: {breaks.sum()} (excluded from trends)")

# 1e. Netflix file: reconcile to the published total before using it.
def read_netflix(sheet):
    # The sheet has 5 rows of title text above the header and a footnote below.
    df = pd.read_excel(RAW / "netflix_engagement_2026H1.xlsx",
                       sheet_name=sheet, header=5, usecols="B:G")
    df["Hours Viewed"] = pd.to_numeric(df["Hours Viewed"], errors="coerce")
    df = df[df["Hours Viewed"].notna()].copy()   # drops blank + footnote rows
    df["type"] = "Show" if sheet == "Shows" else "Movie"
    return df

nf_raw = pd.concat([read_netflix("Shows"), read_netflix("Movies")], ignore_index=True)
total = nf_raw["Hours Viewed"].sum()
gap = (total - NETFLIX_PUBLISHED_TOTAL_HOURS) / NETFLIX_PUBLISHED_TOTAL_HOURS * 100
log(f"Netflix file total: {total/1e9:.1f}B hours vs published 97B+ "
    f"({gap:+.1f}%) -> {'PASS' if abs(gap) < 2 else 'CHECK'}")

missing_date = nf_raw["Release Date"].isna() | (nf_raw["Release Date"] == "*")
log(f"Netflix titles without a release date: {missing_date.mean()*100:.0f}% "
    f"(kept as 'Undated' - mostly licensed titles; dropping them would lose half of viewing)")

# ---------------------------------------------------------------- 2. CLEAN
log("\n=== STAGE 2: CLEAN ===")

gauge["date"] = pd.to_datetime(gauge["month"] + "-01")
gauge["exclude_from_trend"] = breaks

# Tag each Prime Video month: was live sports on the platform?
# Thursday Night Football runs Sept-Dec each year since 2022 (exclusive from Sept 2022);
# events file adds anything else (e.g., NBA playoffs May 2026).
sports_months = set(events.loc[events.event_type.str.contains("Live sports"), "month"])
def sports_flag(row):
    tnf_season = row.date.year >= 2022 and row.date.month in (9, 10, 11, 12)
    return "Live sports month" if (tnf_season or row.month in sports_months) else "No major live sports"
gauge["sports_context"] = gauge.apply(sports_flag, axis=1)
gauge = gauge.merge(events[["month", "event"]], on="month", how="left")

# Netflix: standardize, bucket by release recency (the time-to-market lens).
nf = nf_raw.rename(columns={"Title": "title", "Available Globally?": "global",
                            "Release Date": "release_date", "Hours Viewed": "hours",
                            "Views": "views"})
other_rows = nf.title.isin(["Other Shows", "Other Movies"])   # <50k-view catch-all rows (exact match: real titles can start with "Other")
nf["release_date"] = pd.to_datetime(nf.release_date, errors="coerce")
def recency(d):
    if pd.isna(d): return "Undated (mostly licensed)"
    if d >= pd.Timestamp("2026-01-01"): return "Released in H1 2026"
    if d >= pd.Timestamp("2025-01-01"): return "Released in 2025"
    return "Released before 2025"
nf["release_bucket"] = nf.release_date.apply(recency)
nf.loc[other_rows, "release_bucket"] = "Other (<50K views)"
nf["share_of_hours_pct"] = nf.hours / nf.hours.sum() * 100
log(f"Netflix rows after cleaning: {len(nf)} ({other_rows.sum()} 'Other' catch-all rows kept for 100% coverage)")

# ---------------------------------------------------------------- 3. OUTPUT
log("\n=== STAGE 3: OUTPUT ===")

tv = gauge[gauge.metric.isin(["Streaming", "Cable", "Broadcast"])]
tv[["date", "metric", "share_pct", "source_tier", "exclude_from_trend", "notes", "source"]] \
    .to_csv(OUT / "tableau_tv_category_share.csv", index=False)

plat = gauge[gauge.metric.isin(["Prime Video", "Netflix", "YouTube"])]
plat[["date", "metric", "share_pct", "sports_context", "event", "source_tier", "notes", "source"]] \
    .to_csv(OUT / "tableau_platform_share.csv", index=False)

bucket = (nf.groupby("release_bucket")
            .agg(titles=("title", "count"), hours=("hours", "sum"))
            .assign(share_of_titles_pct=lambda d: d.titles / d.titles.sum() * 100,
                    share_of_hours_pct=lambda d: d.hours / d.hours.sum() * 100)
            .round(1).reset_index())
bucket.to_csv(OUT / "tableau_netflix_release_recency.csv", index=False)

nf[~other_rows].sort_values("hours", ascending=False) \
    [["title", "type", "global", "release_date", "release_bucket", "hours", "views", "share_of_hours_pct"]] \
    .to_csv(OUT / "tableau_netflix_titles.csv", index=False)

# Headline numbers for the memo.
# A plain average of "sports months vs other months" is misleading here: Prime Video's
# baseline grew over time, and our sample skews toward record months. So we report:
#  (a) where every platform record fell, (b) the same-month year-over-year baseline
#  change, and (c) Nielsen's game-day vs monthly-average comparison.
pv = plat[plat.metric == "Prime Video"].sort_values("date")
records, best = [], 0
for _, r in pv.iterrows():
    if r.share_pct > best:
        best = r.share_pct; records.append(f"{r.month} {r.share_pct}% ({r.sports_context})")
log("Prime Video successive highs in sample: " + "; ".join(records))
jun = pv.set_index("month").share_pct
if {"2023-06", "2026-06"} <= set(jun.index):
    log(f"Baseline (June to June): {jun['2023-06']}% -> {jun['2026-06']}% "
        f"(+{jun['2026-06']-jun['2023-06']:.1f} pts) - growth beyond sports peaks")
log("Game-day lift (Nielsen, Oct 2025): 6.4% on TNF Thursdays vs 3.8% monthly average")
log("Netflix viewing by release recency:\n" + bucket.to_string(index=False))

(OUT / "data_quality_report.txt").write_text("\n".join(report))
print("\nDone. Files in output/")
