"""
Earnings-reaction scan via Bloomberg (xbbg).
Run on the Bloomberg terminal PC with the Terminal logged in.

    python earnings_scan.py
    python earnings_scan.py --tickers PRAX,AMAT --start 2026-11-09 --end 2026-11-12

Ranks the tickers by how likely a POSITIVE earnings reaction looks, using a simple
additive score (see SCORING below). Output: earnings_scan.csv + a ranked console table.
Research only, not investment advice.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import argparse
import re
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
from xbbg import blp

warnings.filterwarnings("ignore", category=FutureWarning)   # xbbg "ticker" -> "security" rename notice

# ---------- 1. Config ----------
TICKERS = ["PRAX", "CRWV", "PLUG", "RGTI", "AMAT",
           "TWST", "LCID", "HSAI", "MRCY", "WIX", "ASTS", "ZETA", "ONTO", "HIMS",
           "TTD", "ACHR", "RKLB", "QLYS", "ROIV", "NOVT", "SE"]
WINDOW = ("2026-11-09", "2026-11-12")      # earnings week to rank

N_EVENTS = 8                # past earnings reactions to use
MIN_EVENTS_FOR_RATES = 4    # need at least this many to trust up-rate / beat-rate
REV_MIN = 0.01             # a revision only counts if it moved consensus by more than 1%
UP_RATE_MIN = 0.60
BEAT_RATE_MIN = 0.70
DTC_MIN = 5.0               # days to cover
PC_MAX = 1.0                # put/call ratio
RUN_UP_MAX = 0.25           # 1m return above this = priced in
MIN_EXP_MOVE = 0.08         # expected move below this = not enough payoff
IV_MOVE_DAYS = 7            # straddle proxy horizon: 0.8 * IV * sqrt(days/365)

# Each snapshot field may have several candidate mnemonics; the first one that returns data wins,
# and the run prints which one worked (or that none did, so you can look it up with FLDS).
SNAP = {
    "name":         ["NAME"],
    "price":        ["PX_LAST"],
    "mkt_cap":      ["CUR_MKT_CAP"],
    "high_52w":     ["HIGH_52WEEK"],
    "low_52w":      ["LOW_52WEEK"],
    "report_dt":    ["EXPECTED_REPORT_DT"],
    "target":       ["BEST_TARGET_PRICE"],
    "rec":          ["EQY_REC_CONS"],
    "dtc":          ["SHORT_INT_RATIO"],
    "short_int":    ["SHORT_INT"],
    "short_pct_fl": ["SI_PERCENT_EQUITY_FLOAT", "EQY_SHORT_INT_PCT_FLOAT", "SHORT_INT_PCT_FLOAT"],
    "vol_30d":      ["VOLATILITY_30D"],
    "iv_30d":       ["30DAY_IMPVOL_100.0%MNY_DF"],
    "iv_3m":        ["3MTH_IMPVOL_100.0%MNY_DF"],
    "iv_30d_90":    ["30DAY_IMPVOL_90.0%MNY_DF"],
    "iv_30d_110":   ["30DAY_IMPVOL_110.0%MNY_DF"],
    "pc_oi":        ["PUT_CALL_OPEN_INTEREST_RATIO"],
    "pc_vol":       ["PUT_CALL_VOLUME_RATIO_CUR_DAY"],
}
EARN_HIST_FIELDS = ["EARN_ANN_DT_TIME_HIST_WITH_EPS", "ERN_ANN_DT_AND_PER"]
QTR_PERIODS = ["1BQ", "1FQ"]     # BEST_FPERIOD_OVERRIDE candidates for the next unreported quarter
FY_PERIODS = ["1BF", "1FY"]
EPS_REV_DIRECT = ["BEST_EPS_4WK_CHG", "BEST_EPS_4WK_PCT_CHG", "BEST_EPS_1M_CHG"]


# ---------- 2. xbbg helpers (Narwhals/Arrow -> pandas, long -> wide) ----------
def to_pd(obj):
    """Convert whatever xbbg returns (pandas, Narwhals, Arrow, Polars) to a pandas DataFrame."""
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj
    if hasattr(obj, "to_native"):
        return to_pd(obj.to_native())
    if hasattr(obj, "to_pandas"):
        return obj.to_pandas()
    return pd.DataFrame(obj)


def full(t):
    return f"{t} US Equity"


def bdp_series(tickers, fld, **overrides):
    """One BDP field for all tickers -> Series indexed by ticker, or None if it returned nothing."""
    try:
        raw = to_pd(blp.bdp(tickers, fld, backend="pandas", **overrides))
    except Exception as e:
        print(f"  ! {fld}: {e}")
        return None
    if len(raw) == 0:
        return None
    raw = raw.copy()
    if "ticker" not in raw.columns:
        raw = raw.reset_index()
        raw = raw.rename(columns={raw.columns[0]: "ticker"})
    lower = {str(c).lower(): c for c in raw.columns}
    if {"field", "value"} <= set(lower):                       # long format
        s = raw.groupby("ticker")[lower["value"]].last()
    else:                                                      # wide format
        col = lower.get(fld.lower())
        if col is None:
            others = [c for c in raw.columns if c != "ticker"]
            if len(others) != 1:
                return None
            col = others[0]
        s = raw.set_index("ticker")[col]
    s = s.reindex(tickers)
    return None if s.dropna().empty else s


RESOLVED = {}
PARSE_INFO = {}      # which earnings-history columns were picked (first ticker), for debugging


def resolve(label, candidates, tickers, **overrides):
    """Try candidate mnemonics in order; record which one worked."""
    for fld in candidates:
        s = bdp_series(tickers, fld, **overrides)
        if s is not None:
            RESOLVED[label] = fld
            return s
    RESOLVED[label] = None
    return None


def hist_wide(raw):
    """BDH result (long, wide or MultiIndex) -> DataFrame[date x ticker], numeric, sorted."""
    df = to_pd(raw)
    if len(df) == 0:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    else:
        lower = {str(c).lower(): c for c in df.columns}
        if {"ticker", "date", "value"} <= set(lower):
            df = df.groupby([lower["date"], lower["ticker"]])[lower["value"]].last().unstack()
        elif "date" in lower:
            df = df.set_index(lower["date"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.apply(pd.to_numeric, errors="coerce").sort_index()


def bdh_wide(tickers, fld, start, end, **overrides):
    try:
        return hist_wide(blp.bdh(tickers, fld, start.isoformat(), end.isoformat(),
                                 backend="pandas", **overrides))
    except Exception as e:
        print(f"  ! bdh {fld}: {e}")
        return pd.DataFrame()


# ---------- 3. Estimate revisions ----------
def rev_from_hist(h, days=28):
    """% change in a consensus series over ~4 weeks (denominator |old| so negative EPS behaves)."""
    out = {}
    for t in h.columns:
        s = h[t].dropna()
        if len(s) < 2:
            out[t] = np.nan
            continue
        past = s[s.index <= s.index[-1] - pd.Timedelta(days=days)]
        if len(past):
            ref = past.iloc[-1]
        elif (s.index[-1] - s.index[0]).days >= 14:
            ref = s.iloc[0]
        else:
            out[t] = np.nan
            continue
        out[t] = (s.iloc[-1] - ref) / abs(ref) if ref else np.nan
    return pd.Series(out, dtype=float)


def revisions(label, fld, periods, tickers, today, direct=None):
    """4-week % revision as a decimal. For each period override, tries a direct Bloomberg field (if given),
    then a BDH of the consensus series. Returns the first that produces data."""
    for period in periods:
        ovr = {"BEST_FPERIOD_OVERRIDE": period}
        if direct:
            s = resolve(label, direct, tickers, **ovr)
            if s is not None:
                RESOLVED[label] = f"{RESOLVED[label]} [{period}]"
                return pd.to_numeric(s, errors="coerce") / 100.0     # Bloomberg gives percent
        h = bdh_wide(tickers, fld, today - timedelta(days=40), today, **ovr)
        if not h.empty and h.notna().any().any():
            RESOLVED[label] = f"BDH {fld} [{period}] (computed)"
            return rev_from_hist(h).reindex(tickers)
    RESOLVED[label] = None
    return pd.Series(np.nan, index=tickers)


def level(label, fld, periods, tickers):
    """Consensus level for the first override period that returns data."""
    for period in periods:
        s = resolve(label, [fld], tickers, BEST_FPERIOD_OVERRIDE=period)
        if s is not None:
            RESOLVED[label] = f"{fld} [{period}]"
            return pd.to_numeric(s, errors="coerce")
    RESOLVED[label] = None
    return pd.Series(np.nan, index=tickers)


# ---------- 4. Past earnings reactions ----------
def parse_timing(v):
    """-> 'AMC' (after close), 'BMO' (before open / intraday) or None."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().lower()
    if s in ("", "nan", "nat", "none"):
        return None
    if "after" in s or "aft" in s or "amc" in s or s in ("post", "pm"):
        return "AMC"
    if "before" in s or "bef" in s or "bmo" in s or "dur" in s or s in ("pre", "am"):
        return "BMO"
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        return "AMC" if int(m.group(1)) * 60 + int(m.group(2)) >= 16 * 60 else "BMO"
    return None


def parse_events(raw):
    """bds earnings-history table -> list of {date, timing, beat}, oldest first. Column names are
    matched by keyword because the exact layout differs between xbbg versions."""
    df = to_pd(raw)
    if len(df) == 0:
        return []
    names = {c: str(c).lower().replace(" ", "_") for c in df.columns}

    def find(*keys, excl=()):
        for c, n in names.items():
            if n != "ticker" and any(k in n for k in keys) and not any(x in n for x in excl):
                return c
        return None

    dcol = find("announcement_d", "ann_d", "date", excl=("period",))
    if dcol is None:
        return []
    tcol = find("time", excl=("period",))
    scol = find("surpris")
    acol = find("comparable") or find("actual") or find("eps", excl=("est", "surpris", "time", "consens"))
    ecol = find("estimate", "consensus", "_est", "est_")
    if not PARSE_INFO:
        PARSE_INFO.update(date=dcol, time=tcol, surprise=scol, actual=acol, estimate=ecol,
                          sample=df.head(3).to_string())
    events = []
    for _, r in df.iterrows():
        dt = pd.to_datetime(r[dcol], errors="coerce")
        if pd.isna(dt):
            continue
        beat = None
        if scol is not None:
            v = pd.to_numeric(r[scol], errors="coerce")
            beat = None if pd.isna(v) else bool(v > 0)
        elif acol is not None and ecol is not None:
            a, e = pd.to_numeric(r[acol], errors="coerce"), pd.to_numeric(r[ecol], errors="coerce")
            beat = None if pd.isna(a) or pd.isna(e) else bool(a > e)
        events.append({"date": dt.tz_localize(None).normalize() if dt.tzinfo else dt.normalize(),
                       "timing": parse_timing(r[tcol]) if tcol is not None else None,
                       "beat": beat})
    return sorted(events, key=lambda e: e["date"])


def reaction(close, ev):
    """Close-to-close move around the announcement. After-close: day0 -> day+1. Before-open or intraday:
    day-1 -> day0. Timing unknown: day-1 -> day+1 (captures either)."""
    idx = close.index
    pos = idx.searchsorted(ev["date"])
    if pos >= len(idx) or pos == 0:
        return np.nan
    t = ev["timing"]
    try:
        if t == "AMC":
            if idx[pos] != ev["date"]:
                return np.nan
            return close.iloc[pos + 1] / close.iloc[pos] - 1
        if t == "BMO":
            return close.iloc[pos] / close.iloc[pos - 1] - 1
        return close.iloc[pos + 1] / close.iloc[pos - 1] - 1
    except IndexError:
        return np.nan


def earnings_stats(ticker, close, today):
    """-> dict of reaction stats from the last N_EVENTS announcements."""
    out = dict(n_events=0, avg_move=np.nan, avg_abs_move=np.nan, up_rate=np.nan,
               beat_rate=np.nan, last_move=np.nan, hist_timing=None)
    events, cols_seen = [], None
    for fld in EARN_HIST_FIELDS:
        try:
            raw = to_pd(blp.bds(full(ticker), fld, backend="pandas"))
        except Exception as e:
            print(f"  ! bds {fld} {ticker}: {e}")
            continue
        cols_seen = list(raw.columns)
        events = [e for e in parse_events(raw) if e["date"] < pd.Timestamp(today)]
        if events:
            RESOLVED["earn_hist"] = fld
            break
    if not events:
        out["_cols_seen"] = cols_seen
        return out
    events = events[-N_EVENTS:]
    moves = [(reaction(close, e), e) for e in events]
    moves = [(m, e) for m, e in moves if not np.isnan(m)]
    if not moves:
        return out
    mv = np.array([m for m, _ in moves])
    beats = [e["beat"] for _, e in moves if e["beat"] is not None]
    timings = [e["timing"] for _, e in moves if e["timing"]]
    out.update(n_events=len(mv), avg_move=mv.mean(), avg_abs_move=np.abs(mv).mean(),
               up_rate=(mv > 0).mean(), last_move=mv[-1])
    if len(beats) >= MIN_EVENTS_FOR_RATES:
        out["beat_rate"] = float(np.mean(beats))
    if timings:
        out["hist_timing"] = max(set(timings), key=timings.count)
    return out


# ---------- 5. Scoring ----------
def score_row(r):
    """Simple additive score. Returns (score, signals text, inputs available out of 8)."""
    pts, sig, have = 0, [], 0

    def check(val, cond, plus, label, count=True):
        nonlocal pts, have
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return
        have += count
        if cond(val):
            pts += plus
            sig.append(("+" if plus > 0 else "-") + label)

    check(r["eps_rev_used"], lambda v: v > REV_MIN, 1, "EPSrev")
    check(r["eps_rev_used"], lambda v: v < -REV_MIN, -1, "EPScut", count=False)
    check(r["sales_rev_used"], lambda v: v > REV_MIN, 1, "SALESrev")
    check(r["sales_rev_used"], lambda v: v < -REV_MIN, -1, "SALEScut", count=False)
    up = r["up_rate"] if r["n_events"] >= MIN_EVENTS_FOR_RATES else np.nan
    check(up, lambda v: v > UP_RATE_MIN, 1, "UPrate")
    check(r["beat_rate"], lambda v: v > BEAT_RATE_MIN, 1, "BEATrate")
    check(r["dtc"], lambda v: v > DTC_MIN, 1, "SQUEEZE")
    check(r["pc"], lambda v: v < PC_MAX, 1, "PCbull")
    check(r["ret_1m"], lambda v: v > RUN_UP_MAX, -1, "RUNUP")
    check(r["exp_move"], lambda v: v < MIN_EXP_MOVE, -1, "SMALLmove")
    return pts, " ".join(sig), have


def note(r):
    """Plain-English why / risk for one row."""
    why, risk = [], []
    if r["eps_rev_used"] > REV_MIN:
        why.append(f"EPS estimates ({r['eps_rev_src']}) up {r['eps_rev_used']*100:.1f}% in 4 weeks")
    if r["sales_rev_used"] > REV_MIN:
        why.append(f"sales estimates ({r['sales_rev_src']}) up {r['sales_rev_used']*100:.1f}%")
    if r["n_events"] >= MIN_EVENTS_FOR_RATES and r["up_rate"] > 0.5:
        why.append(f"rose after {r['up_rate']:.0%} of its last {int(r['n_events'])} reports "
                   f"(avg {r['avg_move']*100:+.1f}%)")
    if r["beat_rate"] > BEAT_RATE_MIN:
        why.append(f"beat EPS {r['beat_rate']:.0%} of the time")
    if r["dtc"] > DTC_MIN:
        why.append(f"{r['dtc']:.1f} days to cover (squeeze fuel on a beat)")
    if r["pc"] < PC_MAX:
        why.append(f"options lean bullish (put/call {r['pc']:.2f})")
    if not np.isnan(r["exp_move"]):
        why.append(f"expected move about ±{r['exp_move']*100:.0f}%")
    if r["eps_rev_used"] < -REV_MIN:
        risk.append(f"analysts cutting EPS estimates ({r['eps_rev_used']*100:.1f}% in 4 weeks)")
    if r["sales_rev_used"] < -REV_MIN:
        risk.append(f"analysts cutting sales estimates ({r['sales_rev_used']*100:.1f}% in 4 weeks)")
    if r["ret_1m"] > 0.15:
        risk.append(f"already +{r['ret_1m']*100:.0f}% in 1 month, so good news may be priced in")
    if r["avg_abs_move"] > 0.15:
        risk.append(f"violent two-way history (avg ±{r['avg_abs_move']*100:.0f}% moves)")
    if r["n_events"] >= MIN_EVENTS_FOR_RATES and r["up_rate"] < 0.5:
        risk.append(f"fell after {1 - r['up_rate']:.0%} of past reports")
    if r["dtc"] < 2:
        risk.append("little short interest, so no squeeze fuel")
    if r["off_high"] > -0.05:
        risk.append("trading within 5% of its 52-week high")
    if r["signals_have"] < 5:
        risk.append(f"thin data (only {int(r['signals_have'])}/8 scoring inputs available)")
    if not risk:
        risk.append("reaction is still close to a coin-flip; a beat is already expected")
    return "; ".join(why) or "few positive signals", "; ".join(risk)


# ---------- 6. Main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="comma-separated tickers (without ' US Equity')")
    ap.add_argument("--start", default=WINDOW[0])
    ap.add_argument("--end", default=WINDOW[1])
    args = ap.parse_args()
    tick = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else TICKERS
    full_t = [full(t) for t in tick]
    w0, w1 = pd.Timestamp(args.start), pd.Timestamp(args.end)
    today = date.today()

    print("Fetching snapshot fields...")
    snap = pd.DataFrame(index=full_t)
    for label, cands in SNAP.items():
        s = resolve(label, cands, full_t)
        snap[label] = s if s is not None else np.nan
    if RESOLVED.get("pc_oi") is None:                      # compute put/call from open interest
        puts = bdp_series(full_t, "OPEN_INT_TOTAL_PUT")
        calls = bdp_series(full_t, "OPEN_INT_TOTAL_CALL")
        if puts is not None and calls is not None:
            snap["pc_oi"] = pd.to_numeric(puts, errors="coerce") / pd.to_numeric(calls, errors="coerce")
            RESOLVED["pc_oi"] = "OPEN_INT_TOTAL_PUT / OPEN_INT_TOTAL_CALL (computed)"
    for c in snap.columns:
        if c not in ("name", "report_dt"):
            snap[c] = pd.to_numeric(snap[c], errors="coerce")
    snap["report_dt"] = pd.to_datetime(snap["report_dt"], errors="coerce")

    print("Fetching consensus and revisions...")
    ests = pd.DataFrame(index=full_t)
    ests["eps_q"] = level("eps_q", "BEST_EPS", QTR_PERIODS, full_t)
    ests["sales_q"] = level("sales_q", "BEST_SALES", QTR_PERIODS, full_t)
    ests["eps_fy"] = level("eps_fy", "BEST_EPS", FY_PERIODS, full_t)
    ests["eps_rev_4w"] = revisions("eps_rev_q", "BEST_EPS", QTR_PERIODS, full_t, today, direct=EPS_REV_DIRECT)
    ests["sales_rev_4w"] = revisions("sales_rev_q", "BEST_SALES", QTR_PERIODS, full_t, today)
    ests["eps_fy_rev_4w"] = revisions("eps_rev_fy", "BEST_EPS", FY_PERIODS, full_t, today)
    ests["sales_fy_rev_4w"] = revisions("sales_rev_fy", "BEST_SALES", FY_PERIODS, full_t, today)

    print("Fetching price history...")
    px = bdh_wide(full_t, "PX_LAST", today - timedelta(days=900), today)
    if px.empty:
        raise SystemExit("No price history returned. Is the Terminal logged in?")

    print("Fetching past earnings dates and computing reactions...")
    rows, unparsed, cols_printed = [], [], False
    for t, ft in zip(tick, full_t):
        close = px[ft].dropna() if ft in px else pd.Series(dtype=float)
        st = earnings_stats(t, close, today) if len(close) > 10 else \
            dict(n_events=0, avg_move=np.nan, avg_abs_move=np.nan, up_rate=np.nan,
                 beat_rate=np.nan, last_move=np.nan, hist_timing=None)
        if st["n_events"] == 0:
            unparsed.append(t)
            if st.get("_cols_seen") and not cols_printed:
                print(f"  bds columns seen (paste these if parsing fails): {st['_cols_seen']}")
                cols_printed = True
        ret = lambda n: close.iloc[-1] / close.iloc[-1 - n] - 1 if len(close) > n else np.nan
        rows.append(dict(ticker=t, ret_5d=ret(5), ret_1m=ret(21), ret_3m=ret(63), **st))
    res = pd.DataFrame(rows).set_index(snap.index)
    res = res.drop(columns=[c for c in res.columns if c.startswith("_")], errors="ignore")

    df = snap.join(ests).join(res.drop(columns=["ticker"]))
    df.insert(0, "ticker", tick)
    df["mkt_cap_bn"] = df["mkt_cap"] / 1e9                          # CUR_MKT_CAP is in dollars
    df["off_high"] = df["price"] / df["high_52w"] - 1
    df["upside_to_target"] = df["target"] / df["price"] - 1
    df["pc"] = df["pc_oi"].fillna(df["pc_vol"])
    df["iv"] = df["iv_30d"].fillna(df["iv_3m"])
    df["skew_90_110"] = df["iv_30d_90"] - df["iv_30d_110"]
    df["iv_move"] = 0.8 * (df["iv"] / 100.0) * np.sqrt(IV_MOVE_DAYS / 365.0)   # straddle proxy
    df["exp_move"] = pd.concat([df["iv_move"], df["avg_abs_move"]], axis=1).max(axis=1)
    df["in_window"] = df["report_dt"].between(w0, w1)
    df["timing"] = df["hist_timing"].map({"AMC": "post-mkt", "BMO": "pre-mkt"}).fillna("?")

    # revisions: next-quarter consensus if available, else fiscal-year consensus as a proxy
    def pick(q, fy):
        use_q = q.abs() >= REV_MIN
        val = q.where(use_q, fy.where(fy.notna(), q))
        src = np.where(use_q, "Q", np.where(fy.notna(), "FY", "Q"))
        return val, src
    df["eps_rev_used"], df["eps_rev_src"] = pick(df["eps_rev_4w"], df["eps_fy_rev_4w"])
    df["sales_rev_used"], df["sales_rev_src"] = pick(df["sales_rev_4w"], df["sales_fy_rev_4w"])
    # sanity check: real companies beat EPS most of the time, so a very low median means a mis-parsed column
    df["beat_rate_raw"] = df["beat_rate"]
    if df["beat_rate"].median() < 0.3:
        print("\n!! Median beat rate is %.0f%% -- implausible, so the beat/estimate columns were probably "
              "mis-parsed. beat_rate is ignored in the score (raw values kept as beat_rate_raw)." %
              (df["beat_rate"].median() * 100))
        df["beat_rate"] = np.nan

    sc = df.apply(score_row, axis=1, result_type="expand")
    df["score"], df["signals"], df["signals_have"] = sc[0], sc[1], sc[2]
    df = df.sort_values(["in_window", "score", "up_rate", "avg_abs_move"],
                        ascending=[False, False, False, False])
    df["rank"] = np.where(df["in_window"], np.arange(1, len(df) + 1), np.nan)

    keep = ["rank", "ticker", "name", "report_dt", "timing", "in_window", "price", "mkt_cap_bn",
            "ret_5d", "ret_1m", "ret_3m", "off_high", "upside_to_target", "rec", "eps_q", "eps_rev_4w", "eps_rev_used", "eps_rev_src", "sales_rev_used", "sales_rev_src",
            "sales_q", "sales_rev_4w", "sales_fy_rev_4w", "eps_fy", "eps_fy_rev_4w", "n_events", "up_rate", "beat_rate", "beat_rate_raw",
            "avg_move", "avg_abs_move", "last_move", "iv", "iv_move", "exp_move", "skew_90_110",
            "dtc", "short_pct_fl", "pc_oi", "pc_vol", "vol_30d", "score", "signals", "signals_have"]
    df[[c for c in keep if c in df]].to_csv("earnings_scan.csv", index=False)

    # ---- console table ----
    pct = lambda v: "" if pd.isna(v) else f"{v*100:+.1f}"
    pct0 = lambda v: "" if pd.isna(v) else f"{v*100:.0f}"
    num = lambda v, f="{:.1f}": "" if pd.isna(v) else f.format(v)
    show = pd.DataFrame({
        "rank": df["rank"].map(lambda v: "-" if pd.isna(v) else f"{int(v)}"),
        "ticker": df["ticker"],
        "report": df["report_dt"].dt.strftime("%d %b").fillna("?"),
        "when": df["timing"],
        "price": df["price"].map(lambda v: num(v, "{:.2f}")),
        "cap$bn": df["mkt_cap_bn"].map(lambda v: num(v, "{:.2f}")),
        "1m%": df["ret_1m"].map(pct),
        "offHi%": df["off_high"].map(pct),
        "EPSrev%": df["eps_rev_used"].map(pct),
        "SLSrev%": df["sales_rev_used"].map(pct),
        "up%": df["up_rate"].map(pct0),
        "beat%": df["beat_rate"].map(pct0),
        "avg|mv|%": df["avg_abs_move"].map(lambda v: "" if pd.isna(v) else f"{v*100:.1f}"),
        "exp%": df["exp_move"].map(lambda v: "" if pd.isna(v) else f"{v*100:.1f}"),
        "DTC": df["dtc"].map(num),
        "P/C": df["pc"].map(lambda v: num(v, "{:.2f}")),
        "score": df["score"].astype(int),
        "signals": df["signals"],
    })
    print(f"\nRanked by P(positive reaction), window {w0:%d %b}-{w1:%d %b %Y}. "
          f"Out-of-window names are listed last (rank '-').\n")
    print(show.to_string(index=False))

    pool = df[df["in_window"]] if df["in_window"].any() else df
    print("\n=== Top 5 notes ===")
    for _, r in pool.head(5).iterrows():
        why, risk = note(r)
        print(f"\n{r['ticker']} (score {int(r['score'])}, {r['signals'] or 'no signals'})")
        print(f"  Why:  {why}.")
        print(f"  Risk: {risk}.")

    print("\nEarnings-history columns picked: " +
          ", ".join(f"{k}={v}" for k, v in PARSE_INFO.items() if k != "sample") +
          "\nFirst rows of the earnings-history table (paste this if beat rates look wrong):\n" +
          PARSE_INFO.get("sample", "(none)"))

    out_of = df[~df["in_window"]]
    if len(out_of):
        def when(r):
            return "?" if pd.isna(r["report_dt"]) else r["report_dt"].strftime("%d %b")
        print("\nNOT in the window per EXPECTED_REPORT_DT: " +
              ", ".join(f"{r['ticker']} ({when(r)})" for _, r in out_of.iterrows()))
    if unparsed:
        print("\nNo earnings history parsed for: " + ", ".join(unparsed) +
              " (up-rate / avg move / beat-rate blank for these).")

    print("\n=== Field resolution (which Bloomberg mnemonic worked) ===")
    missing = []
    for k, v in RESOLVED.items():
        print(f"  {k:<14} {v if v else 'UNRESOLVED'}")
        if not v:
            missing.append(k)
    if missing:
        print("\nUnresolved: " + ", ".join(missing) +
              ". On the Terminal run FLDS <keyword> (e.g. FLDS SHORT INTEREST PERCENT FLOAT, "
              "FLDS PUT CALL RATIO, FLDS IMPLIED VOLATILITY 30 DAY, FLDS EPS 4 WEEK CHANGE) and "
              "send me the mnemonic.")
    print("\nSaved: earnings_scan.csv")
    print("Scoring: +1 each for EPS rev>+1%, sales rev>+1%, up-rate>60%, beat-rate>70%, DTC>5, put/call<1; "
          "-1 each for EPS or sales rev<-1%, 1m return>25%, expected move<8%. "
          "Blank inputs score 0 (see signals_have in the CSV).")
    print("Research only, not investment advice.")


if __name__ == "__main__":
    main()
