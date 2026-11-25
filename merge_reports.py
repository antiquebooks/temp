#!/usr/bin/env python3
"""
merge_reports.py - merge PV portal "Plant Report" XLSX exports into clean CSVs.

Handles two report types, auto-detected per file from the content:
  * HOURLY  : one row per hour ("2026-09-01 07:00:00 DST")  -> from the "daily" exports
  * DAILY   : one row per day  ("2026-03-26")               -> from the "monthly" exports

Usage
-----
  python merge_reports.py <folder_or_files...> [-o OUTDIR] [--tz Europe/Bratislava]

Writes into OUTDIR (default: ./merged):
  hourly.csv      all hourly rows, sorted, de-duplicated, with derived consumption
  daily.csv       all daily rows, sorted, de-duplicated
  report.txt      what was read, skipped, duplicated, and which days/hours are missing
  reconcile.csv   (only if both types are present) daily sums of hourly data vs daily report

Requires: pandas, openpyxl.
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# Portal header -> clean column name. Unknown headers are slugified automatically.
COLUMN_MAP = {
    "statistical period": "period",
    "total string capacity (kwp)": "string_capacity_kwp",
    "global irradiation (kwh/㎡)": "irradiation_kwh_m2",
    "sun hours (h)": "sun_hours",
    "average temperature(°c)": "avg_temp_c",
    "theoretical yield (kwh)": "theoretical_yield_kwh",
    "pv yield (kwh)": "pv_yield_kwh",
    "inverter yield (kwh)": "inverter_yield_kwh",
    "total yield (kwh)": "total_yield_cumulative_kwh",
    "export (kwh)": "export_kwh",
    "import (kwh)": "import_kwh",
    "specific energy (kwh/kwp)": "specific_energy_kwh_kwp",
    "loss due to export limitation (kwh)": "export_limit_loss_kwh",
    "loss due to export limitation(€)": "export_limit_loss_eur",
    "consumption (kwh)": "consumption_kwh",
    "self-consumption (kwh)": "self_consumption_kwh",
    "self-consumption rate (%)": "self_consumption_rate_pct",
    "peak power (kw)": "peak_power_kw",
    "performance ratio(%)": "performance_ratio_pct",
    "co₂ avoided (t)": "co2_avoided_t",
    "standard coal saved (t)": "coal_saved_t",
    "charge (kwh)": "battery_charge_kwh",
    "discharge (kwh)": "battery_discharge_kwh",
    "revenue (€)": "revenue_raw",  # scale looks off vs. kWh; kept as-is, do not use for ROI
}
NUMERIC_META_COLS = {"period", "timestamp_local", "timestamp_utc", "tz_label", "date", "source_file"}


def slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower()).strip("_")
    return s or "col"


def clean_columns(cols):
    out = []
    for c in cols:
        key = str(c).strip().lower()
        out.append(COLUMN_MAP.get(key, slug(c)))
    return out


def find_header_row(raw: pd.DataFrame) -> int:
    """Row index whose first cell is 'Statistical Period'."""
    for i in range(min(10, len(raw))):
        if str(raw.iat[i, 0]).strip().lower() == "statistical period":
            return i
    raise ValueError("no 'Statistical Period' header row found")


def read_report(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    h = find_header_row(raw)
    df = raw.iloc[h + 1:].copy()
    df.columns = clean_columns(raw.iloc[h].tolist())
    df = df.dropna(how="all")
    df = df[df["period"].notna() & (df["period"].astype(str).str.strip() != "")]
    # blanks -> NaN, numbers -> float
    for c in df.columns:
        if c != "period":
            df[c] = pd.to_numeric(df[c].replace("", pd.NA), errors="coerce")
    df["period"] = df["period"].astype(str).str.strip()
    df["source_file"] = path.name
    return df.reset_index(drop=True)


def detect_kind(df: pd.DataFrame) -> str:
    # hourly periods contain a time of day; daily ones are plain dates
    return "hourly" if df["period"].str.contains(r"\d{1,2}:\d{2}").any() else "daily"


def parse_hourly(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """'2026-09-01 07:00:00 DST' -> local time, tz label, and UTC (DST-aware)."""
    m = df["period"].str.extract(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}(?::\d{2})?)\s*([A-Za-z]*)$")
    bad = m[0].isna()
    if bad.any():
        print(f"  warning: {bad.sum()} unparseable timestamps dropped, e.g. {df.loc[bad, 'period'].iloc[0]!r}")
    df, m = df[~bad].copy(), m[~bad]
    df["timestamp_local"] = pd.to_datetime(m[0])
    df["tz_label"] = m[1].values
    is_dst = (df["tz_label"].str.upper() == "DST").to_numpy()
    # the DST flag resolves the repeated hour on the autumn clock change
    try:
        aware = df["timestamp_local"].dt.tz_localize(tz, ambiguous=is_dst, nonexistent="shift_forward")
        df["timestamp_utc"] = aware.dt.tz_convert("UTC").dt.tz_localize(None)
    except Exception as e:  # keep going with local time only
        print(f"  warning: could not compute UTC ({e}); timestamp_utc left empty")
        df["timestamp_utc"] = pd.NaT
    df["date"] = df["timestamp_local"].dt.date
    return df.drop(columns="period")


def parse_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["period"], errors="coerce").dt.date
    bad = df["date"].isna()
    if bad.any():
        print(f"  warning: {bad.sum()} unparseable dates dropped")
    return df[~bad].drop(columns="period")


def value_cols(df):
    return [c for c in df.columns if c not in NUMERIC_META_COLS]


def dedupe(df: pd.DataFrame, key: str, log: list) -> pd.DataFrame:
    """Keep one row per key: the one with most filled values; later file wins ties."""
    vc = value_cols(df)
    df = df.copy()
    df["_filled"] = df[vc].notna().sum(axis=1)
    df["_order"] = range(len(df))
    dup = df[df.duplicated(key, keep=False)]
    if len(dup):
        # report only real conflicts (same key, different numbers)
        conflicts = 0
        for _, g in dup.groupby(key):
            vals = g[vc].dropna(axis=1, how="all").round(4)
            if len(vals.drop_duplicates()) > 1:
                conflicts += 1
        log.append(f"{key}: {dup[key].nunique()} keys appear in several files "
                   f"({conflicts} with differing values; richest row kept).")
    df = df.sort_values(["_filled", "_order"]).drop_duplicates(key, keep="last")
    return df.drop(columns=["_filled", "_order"]).sort_values(key).reset_index(drop=True)


def drop_empty_rows(df: pd.DataFrame, key: str, log: list) -> pd.DataFrame:
    vc = [c for c in value_cols(df) if c != "revenue_raw"]
    empty = df[vc].isna().all(axis=1)
    if empty.any():
        log.append(f"{key}: {empty.sum()} rows had no data at all (dropped): "
                   + _ranges(df.loc[empty, key]))
    return df[~empty].reset_index(drop=True)


def _ranges(series) -> str:
    """Compress a list of dates/timestamps into 'a..b, c' text."""
    vals = sorted(pd.to_datetime(series).dt.normalize().unique())
    if not vals:
        return "-"
    out, start, prev = [], vals[0], vals[0]
    for v in vals[1:]:
        if (v - prev).days > 1:
            out.append((start, prev)); start = v
        prev = v
    out.append((start, prev))
    return ", ".join(f"{a:%Y-%m-%d}" if a == b else f"{a:%Y-%m-%d}..{b:%Y-%m-%d}" for a, b in out)


def add_hourly_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Hourly exports have no consumption column; it follows from the energy balance:
       consumption = PV - export + import   (matches the daily report exactly)."""
    if {"pv_yield_kwh", "export_kwh", "import_kwh"} <= set(df.columns):
        df["self_consumption_kwh"] = (df["pv_yield_kwh"] - df["export_kwh"]).clip(lower=0).round(3)
        df["consumption_kwh"] = (df["self_consumption_kwh"] + df["import_kwh"]).round(3)
    return df


def coverage(df, key, freq, log, label):
    if df.empty:
        return
    if key == "timestamp_local":
        full = pd.date_range(df[key].min().normalize(), df[key].max().normalize() + pd.Timedelta(hours=23), freq="h")
        have = pd.DatetimeIndex(df[key])
        miss_days = sorted({d for d in full.difference(have).normalize()})
        log.append(f"{label}: {len(df)} rows, {df[key].min()} -> {df[key].max()}")
        if miss_days:
            log.append(f"{label}: days with missing hours: {_ranges(pd.Series(miss_days))} "
                       f"(autumn clock change legitimately has 25 h / spring 23 h)")
    else:
        d = pd.to_datetime(df["date"])
        full = pd.date_range(d.min(), d.max(), freq="D")
        miss = full.difference(pd.DatetimeIndex(d))
        log.append(f"{label}: {len(df)} days, {d.min():%Y-%m-%d} -> {d.max():%Y-%m-%d}")
        if len(miss):
            log.append(f"{label}: {len(miss)} calendar days missing: {_ranges(pd.Series(miss))}")


def reconcile(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in ["pv_yield_kwh", "export_kwh", "import_kwh", "consumption_kwh"]
            if c in hourly.columns and c in daily.columns]
    h = hourly.groupby("date")[cols].sum(min_count=1)
    d = daily.set_index("date")[cols]
    j = h.join(d, lsuffix="_hourly_sum", rsuffix="_daily_report", how="inner")
    for c in cols:
        j[f"{c}_diff"] = (j[f"{c}_hourly_sum"] - j[f"{c}_daily_report"]).round(3)
    return j.reset_index()


def collect(paths):
    files = []
    for p in paths:
        p = Path(p)
        files += sorted(p.rglob("*.xlsx")) if p.is_dir() else [p]
    return [f for f in files if not f.name.startswith("~$")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="folder(s) and/or .xlsx files")
    ap.add_argument("-o", "--out", default="merged", help="output folder (default: merged)")
    ap.add_argument("--tz", default="Europe/Bratislava", help="local timezone of the plant (for UTC column)")
    args = ap.parse_args()

    log, hourly_parts, daily_parts = [], [], []
    files = collect(args.inputs)
    if not files:
        sys.exit("no .xlsx files found")

    for f in files:
        try:
            df = read_report(f)
            kind = detect_kind(df)
            df = parse_hourly(df, args.tz) if kind == "hourly" else parse_daily(df)
            (hourly_parts if kind == "hourly" else daily_parts).append(df)
            print(f"{f.name}: {kind}, {len(df)} rows")
        except Exception as e:
            log.append(f"SKIPPED {f.name}: {e}")
            print(f"{f.name}: SKIPPED ({e})")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    hourly = daily = None

    if hourly_parts:
        hourly = pd.concat(hourly_parts, ignore_index=True)
        hourly = dedupe(hourly, "timestamp_utc" if hourly["timestamp_utc"].notna().all() else "timestamp_local", log)
        hourly = drop_empty_rows(hourly, "timestamp_local", log)
        hourly = add_hourly_derived(hourly).sort_values("timestamp_local")
        lead = ["timestamp_local", "timestamp_utc", "tz_label", "date"]
        hourly = hourly[lead + [c for c in hourly.columns if c not in lead + ["source_file"]] + ["source_file"]]
        hourly = hourly.dropna(axis=1, how="all")
        hourly.to_csv(out / "hourly.csv", index=False)
        coverage(hourly, "timestamp_local", "h", log, "HOURLY")

    if daily_parts:
        daily = pd.concat(daily_parts, ignore_index=True)
        daily = dedupe(daily, "date", log)
        daily = drop_empty_rows(daily, "date", log)
        lead = ["date"]
        daily = daily[lead + [c for c in daily.columns if c not in lead + ["source_file"]] + ["source_file"]]
        daily = daily.dropna(axis=1, how="all").sort_values("date")
        daily.to_csv(out / "daily.csv", index=False)
        coverage(daily, "date", "D", log, "DAILY")

    if hourly is not None and daily is not None:
        rec = reconcile(hourly.assign(date=pd.to_datetime(hourly["date"]).dt.date), daily)
        if len(rec):
            rec.to_csv(out / "reconcile.csv", index=False)
            diffcols = [c for c in rec.columns if c.endswith("_diff")]
            worst = rec[diffcols].abs().max().max()
            log.append(f"RECONCILE: {len(rec)} days present in both; max |hourly sum - daily report| = {worst} kWh")

    (out / "report.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    print("\n" + "\n".join(log))
    print(f"\nOutput written to {out.resolve()}")


if __name__ == "__main__":
    main()
