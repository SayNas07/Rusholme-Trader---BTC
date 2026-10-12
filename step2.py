"""
Catalyst watchlist analysis via Bloomberg (xbbg).
Run on the Bloomberg terminal PC with the Terminal logged in.
Output: watchlist_snapshot.csv, watchlist_prices.csv, watchlist_summary.csv
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from datetime import date, timedelta
import pandas as pd
import numpy as np
from xbbg import blp

# ---------- 1. Watchlist (ticker -> (group, catalyst)) ----------
WATCH = {
    # Unknown Phase 3 results (PRAX/COGT-type)
    "IVA US Equity":  ("Phase 3 readout", "NATiV3 MASH topline, Q4 2026"),
    "ADCT US Equity": ("Phase 3 readout", "LOTIS-5 DLBCL + BLA, end-2026"),
    # Brazil runoff 25 Oct (Argentina-type)
    "ITUB US Equity": ("Brazil runoff", "Runoff 25 Oct"),
    "BBD US Equity":  ("Brazil runoff", "Runoff 25 Oct"),
    "NU US Equity":   ("Brazil runoff", "Runoff 25 Oct"),
    "PBR US Equity":  ("Brazil runoff", "Runoff 25 Oct"),
    "VALE US Equity": ("Brazil runoff", "Runoff 25 Oct"),
    # Takeover candidates (Cidara-type)
    "RVMD US Equity": ("Takeover", "Reported Merck interest"),
    "ABVX US Equity": ("Takeover", "Obefazimod UC"),
    "GPCR US Equity": ("Takeover", "Oral GLP-1"),
    "VKTX US Equity": ("Takeover", "VK2735 obesity"),
    "TVTX US Equity": ("Takeover", "Filspari FSGS"),
    "MPLT US Equity": ("Takeover", "ML-007 schizophrenia"),
    "QGEN US Equity": ("Takeover", "Strategic review"),
    # Known FDA dates (lower upside, comparison)
    "AGIO US Equity": ("FDA decision", "PDUFA 1 Nov"),
    "CYTK US Equity": ("FDA decision", "PDUFA 14 Nov"),
    "SMMT US Equity": ("FDA decision", "PDUFA 14 Nov"),
    "CAPR US Equity": ("FDA decision", "PDUFA 22 Nov"),
    "MRNA US Equity": ("Data event", "ESMO 24 Oct"),
    # Benchmark: last year's winner
    "PRAX US Equity": ("Benchmark", "2025 winner"),
}
TICKERS = list(WATCH)

# ---------- 2. Snapshot fields (fetched one by one so a bad field can't break the run) ----------
FIELDS = {
    "NAME": "name",
    "GICS_SECTOR_NAME": "sector",
    "PX_LAST": "price",
    "CUR_MKT_CAP": "mkt_cap",
    "HIGH_52WEEK": "high_52w",
    "LOW_52WEEK": "low_52w",
    "BEST_TARGET_PRICE": "analyst_target",
    "EQY_REC_CONS": "rec_consensus",       # 1 = sell ... 5 = buy
    "TOT_ANALYST_REC": "n_analysts",
    "SHORT_INT_RATIO": "days_to_cover",
    "BS_CASH_NEAR_CASH_ITEM": "cash",
    "CF_FREE_CASH_FLOW": "fcf_ltm",
    "VOLATILITY_30D": "hist_vol_30d",
    "EXPECTED_REPORT_DT": "next_earnings",
}


def long_to_wide(df):
    """xbbg may return long (ticker, field, value) or wide; normalise to wide."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(df)
    if {"ticker", "field", "value"}.issubset(df.columns):
        return df.pivot(index="ticker", columns="field", values="value")
    return df


print("Fetching snapshot...")
parts = []
for fld in FIELDS:
    try:
        parts.append(long_to_wide(blp.bdp(TICKERS, fld)))
    except Exception as e:
        print(f"  skipped {fld}: {e}")
snap = pd.concat(parts, axis=1) if parts else pd.DataFrame(index=TICKERS)
snap.columns = [FIELDS.get(str(c).upper(), str(c)) for c in snap.columns]
snap = snap.reindex(TICKERS)
for c in ["price", "mkt_cap", "high_52w", "low_52w", "analyst_target",
          "rec_consensus", "n_analysts", "days_to_cover", "cash", "fcf_ltm", "hist_vol_30d"]:
    if c in snap:
        snap[c] = pd.to_numeric(snap[c], errors="coerce")

# ---------- 3. One year of daily prices ----------
print("Fetching price history...")
end = date.today()
start = end - timedelta(days=370)
px = pd.DataFrame(blp.bdh(TICKERS, "PX_LAST", start.isoformat(), end.isoformat()))
if isinstance(px.columns, pd.MultiIndex):        # (ticker, field) columns
    px.columns = px.columns.get_level_values(0)
elif {"ticker", "date", "value"}.issubset(px.columns):  # long format
    px = px.pivot(index="date", columns="ticker", values="value")
px.index = pd.to_datetime(px.index)
px = px.apply(pd.to_numeric, errors="coerce").sort_index()


def ret(s, days):
    s = s.dropna()
    if len(s) < 2:
        return np.nan
    past = s[s.index <= s.index[-1] - pd.Timedelta(days=days)]
    return s.iloc[-1] / past.iloc[-1] - 1 if len(past) else np.nan


hist = pd.DataFrame({
    "ret_1m": {t: ret(px[t], 30) for t in px},
    "ret_3m": {t: ret(px[t], 91) for t in px},
    "ret_6m": {t: ret(px[t], 182) for t in px},
    "ret_1y": {t: ret(px[t], 365) for t in px},
})

# ---------- 4. Combine + derived metrics ----------
out = snap.join(hist)
out.insert(0, "group", [WATCH[t][0] for t in out.index])
out.insert(1, "catalyst", [WATCH[t][1] for t in out.index])
out["mkt_cap_bn"] = out["mkt_cap"] / 1e9 if "mkt_cap" in out else np.nan
if "high_52w" in out:
    out["off_52w_high"] = out["price"] / out["high_52w"] - 1
if "analyst_target" in out:
    out["upside_to_target"] = out["analyst_target"] / out["price"] - 1
if "cash" in out and "mkt_cap" in out:
    out["cash_pct_mktcap"] = out["cash"] / out["mkt_cap"]

# Flags based on our filter (non-penny, ideally $0.5-5bn for 2-4x potential)
def flags(r):
    f = []
    if r.get("price", np.nan) < 5:
        f.append("PENNY<$5")
    cap = r.get("mkt_cap_bn", np.nan)
    if cap < 0.5:
        f.append("MICROCAP")
    elif cap > 20:
        f.append("LARGE-2x unlikely")
    elif 0.5 <= cap <= 5:
        f.append("SWEET-SPOT cap")
    if r.get("ret_3m", 0) > 0.5:
        f.append("ALREADY RAN >50% 3M")
    if r.get("cash_pct_mktcap", 0) > 0.5:
        f.append("CASH-RICH")
    return ", ".join(f)

out["flags"] = out.apply(flags, axis=1)

# ---------- 5. Save + print ----------
snap.to_csv("watchlist_snapshot.csv")
px.to_csv("watchlist_prices.csv")
cols = ["group", "catalyst", "name", "price", "mkt_cap_bn", "ret_1m", "ret_3m", "ret_1y",
        "off_52w_high", "upside_to_target", "rec_consensus", "days_to_cover",
        "cash_pct_mktcap", "hist_vol_30d", "next_earnings", "flags"]
cols = [c for c in cols if c in out]
out[cols].to_csv("watchlist_summary.csv")

pd.set_option("display.width", 250, "display.max_columns", 30)
pct = ["ret_1m", "ret_3m", "ret_1y", "off_52w_high", "upside_to_target", "cash_pct_mktcap"]
show = out[cols].copy()
for c in pct:
    if c in show:
        show[c] = (show[c] * 100).round(1)
for g, grp in show.groupby("group", sort=False):
    print(f"\n=== {g} ===")
    print(grp.drop(columns=["group"]).round(2).to_string())

print("\nSaved: watchlist_summary.csv, watchlist_snapshot.csv, watchlist_prices.csv")