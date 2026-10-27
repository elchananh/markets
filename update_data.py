#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh data for the market-overview site (general market research).

Usage:
    python3 update_data.py                    # all markets in config/markets.py
    python3 update_data.py brazil japan       # selected markets
    python3 update_data.py --no-fundamentals  # skip per-stock Yahoo .info calls (faster)
    python3 update_data.py --build-only       # no downloads: re-compact existing data/*.json and rebuild data/markets.json
    python3 update_data.py --standalone       # also build markets_standalone.html (single offline file, large; not deployed)

Writes:
    data/<market>.json     one file per market, loaded on demand by index.html (fetch over http/https)
    data/markets.json      list of markets with data
Series are stored daily for the last 5 years (+1 month) and weekly (last close of each week) before that,
so every range up to 5Y and every return in the tables is computed from daily closes, and each file stays small.
Returns/volatility tables are computed in Python from the full daily history before thinning.
If a run cannot fetch the fund holdings or the index for a market, the previous data for that part is kept
(and labelled with its own as-of date) instead of being overwritten with nothing.
Runs locally or in GitHub Actions (.github/workflows/refresh.yml); only paths inside this folder are used.

Rules: no fabricated numbers; anything not fetched is recorded in "gaps" and shown as 'לא זמין'. No forecasts.
"""
import sys, os, io, re, json, math, time, datetime as dt, traceback
import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "config"))
from markets import MARKETS, SECTOR_ETF_NAMES  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
CACHE_DIR = os.path.join(ROOT, "data", "cache")
INFO_TTL_DAYS = 3
PROXY_YEARS = 10
PROXY_MIN_COVERAGE = 0.5
PROXY_MAX_MEMBERS = 30
TOP_N = 5
SCREENER = ("https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/"
            "product-screener-v3/data/en/us-ishares/ishares-product-screener-backend-config&siteEntryPassthrough=true")

SECTOR_HE = {
    "Financials": "פיננסים", "Energy": "אנרגיה", "Materials": "חומרי גלם", "Utilities": "תשתיות ושירותים (Utilities)",
    "Industrials": "תעשייה", "Consumer Staples": "צריכה בסיסית", "Consumer Discretionary": "צריכה מחזורית",
    "Health Care": "בריאות", "Information Technology": "טכנולוגיית מידע", "Communication": "תקשורת",
    "Communication Services": "תקשורת", "Real Estate": "נדל\"ן", "Cash and/or Derivatives": "מזומן ונגזרים",
    "Other": "אחר", "Unclassified": "לא מסווג",
}
# Yahoo (Morningstar) sector names -> GICS-like names used by fund providers
YAHOO_TO_GICS = {"Financial Services": "Financials", "Basic Materials": "Materials", "Consumer Cyclical": "Consumer Discretionary",
                 "Consumer Defensive": "Consumer Staples", "Healthcare": "Health Care", "Technology": "Information Technology",
                 "Communication Services": "Communication Services", "Industrials": "Industrials", "Energy": "Energy",
                 "Utilities": "Utilities", "Real Estate": "Real Estate"}
# iShares "Exchange" column -> Yahoo suffix
EXCH_SUFFIX = {
    "XBSP": ".SA", "NYSE": "", "NASDAQ": "", "Nasdaq": "", "Cboe BZX": "", "NYSE Arca": "", "NYSE MKT LLC": "",
    "Hong Kong Exchanges And Clearing Ltd": ".HK", "Shanghai Stock Exchange": ".SS", "Shenzhen Stock Exchange": ".SZ",
    "Santiago Stock Exchange": ".SN", "Bse Ltd": ".BO", "National Stock Exchange Of India": ".NS",
    "Singapore Exchange": ".SI", "Tel Aviv Stock Exchange": ".TA", "Tokyo Stock Exchange": ".T",
    "Bolsa Mexicana De Valores": ".MX", "Warsaw Stock Exchange/Equities/Main Market": ".WA",
    "Korea Exchange (Kosdaq)": ".KQ", "Korea Exchange (Stock Market)": ".KS", "Gretai Securities Market": ".TWO",
    "Taiwan Stock Exchange": ".TW", "London Stock Exchange": ".L", "Stock Exchange Of Thailand": ".BK",
    "Asx - All Markets": ".AX", "Bolsa De Valores De Colombia": ".CL", "Bolsa De Valores De Lima": ".LM",
    "Toronto Stock Exchange": ".TO", "Athens Exchange S.A. Cash Market": ".AT", "Xetra": ".DE",
}
# Global X / Bloomberg exchange codes -> (Yahoo suffix, currency)
BBG_SUFFIX = {"GA": (".AT", "EUR"), "CN": (".TO", "CAD"), "CI": (".SN", "CLP"), "CB": (".CL", "COP"), "LN": (".L", "GBP"),
              "AR": (".BA", "ARS"), "US": ("", "USD"), "SP": (".SI", "SGD"), "HK": (".HK", "HKD"), "PE": (".LM", "PEN")}

# Provider ticker -> Yahoo symbol where the mechanical mapping differs (each verified to have Yahoo history)
ALIAS = {"ECOPETL.CL": "ECOPETROL.CL", "PFDAVIGR.CL": "PFDAVVNDA.CL", "PFGRUPSU.CL": "PFGRUPSURA.CL", "GRUPOSUR.CL": "GRUPOSURA.CL",
         "GRUPOARG.CL": "GRUPOARGOS.CL", "CORFICOL.CL": "CORFICOLCF.CL", "PFGRUPOA.CL": "PFGRUPOARG.CL", "ANDINAB.SN": "ANDINA-B.SN",
         "BAJAJ.AUTO.NS": "BAJAJ-AUTO.NS", "SHFL.NS": "SHRIRAMFIN.NS", "CLAR.SI": "A17U.SI", "CICT.SI": "C38U.SI"}

def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")

def log(*a):
    print("[update]", *a, flush=True)

def http_get(url, timeout=60):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            return r
        except Exception as e:
            log("  http retry", url, e); time.sleep(3)
    raise RuntimeError("HTTP failed: " + url)

# ================================================================ fund providers
_SCREENER = None
def ishares_page(fund):
    global _SCREENER
    if _SCREENER is None:
        _SCREENER = {}
        try:
            for pid, v in http_get(SCREENER).json().items():
                if v.get("localExchangeTicker"):
                    _SCREENER[v["localExchangeTicker"]] = "https://www.ishares.com" + v["productPageUrl"]
        except Exception as e:
            log("screener failed", e)
    return _SCREENER.get(fund)

_FUND_CACHE = {}
def fetch_ishares(fund):
    if fund in _FUND_CACHE:
        return _FUND_CACHE[fund]
    page = ishares_page(fund)
    if not page:
        raise RuntimeError(f"קרן {fund} לא נמצאה ברשימת המוצרים של iShares")
    url = page + "/latest-holdings.csv"
    r = http_get(url); r.raise_for_status()
    txt = r.content.decode("utf-8-sig", errors="replace")
    if txt.lstrip().startswith("<"):
        raise RuntimeError("iShares החזירה HTML במקום CSV")
    lines = txt.splitlines()
    as_of = next((ln.split(",", 1)[1].strip().strip('"') for ln in lines[:10] if ln.startswith("Fund Holdings as of")), None)
    hdr = next(i for i, ln in enumerate(lines) if ln.startswith("Ticker,"))
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])), dtype=str, on_bad_lines="skip")
    df = df[df["Ticker"].notna() & df["Name"].notna()].copy()
    if "Weight (%)" not in df and "Market Weight" in df:   # newer iShares CSV layout (e.g. IVV since Oct 2026)
        df["Weight (%)"] = df["Market Weight"]
    for c in ["Weight (%)", "Market Value", "Price"]:
        if c in df:
            df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce")
    out = {"provider": "iShares", "fund": fund, "fund_name": lines[0].strip().strip('"'), "as_of": as_of,
           "url": url, "page": page, "df": df}
    _FUND_CACHE[fund] = out
    return out

def ishares_members(f):
    df = f["df"]
    eq = df[df["Asset Class"] == "Equity"].copy()
    def ysym(row):
        ex, t = row.get("Exchange"), str(row["Ticker"]).strip()
        if ex not in EXCH_SUFFIX or t in ("-", "") or t.endswith("D") and ex == "NO MARKET (E.G. UNLISTED)":
            return None
        suf = EXCH_SUFFIX[ex]
        t = t.replace("*", "").rstrip(".")
        if suf == ".HK":
            t = t.zfill(4) if t.isdigit() else t
        elif suf == ".BK":
            t = re.sub(r"\.R$|\.F$", "", t)
        elif suf in ("", ".TO", ".SN", ".L"):
            t = t.replace(" ", "-").replace(".", "-")
        elif suf == ".T" or suf == ".KS" or suf == ".KQ" or suf == ".TW" or suf == ".TWO":
            pass
        t = t.replace("/", "-")
        return ALIAS.get(t + suf, t + suf)
    eq["yahoo"] = eq.apply(ysym, axis=1)
    eq["ccy"] = eq["Market Currency"].replace({"CNH": "CNY", "GBX": "GBP"})
    return pd.DataFrame({"ticker": eq["Ticker"], "name": eq["Name"], "sector": eq["Sector"], "weight": eq["Weight (%)"],
                         "yahoo": eq["yahoo"], "ccy": eq["ccy"], "location": eq["Location"]})

def fetch_globalx(fund):
    if fund in _FUND_CACHE:
        return _FUND_CACHE[fund]
    page = f"https://www.globalxetfs.com/funds/{fund.lower()}/"
    csv_url, txt = None, None
    for k in range(0, 12):
        d = (dt.date.today() - dt.timedelta(days=k)).strftime("%Y%m%d")
        u = f"https://assets.globalxetfs.com/funds/holdings/{fund.lower()}_full-holdings_{d}.csv"
        r = http_get(u)
        if r.status_code == 200 and not r.text.lstrip().startswith("<"):
            csv_url, txt = u, r.content.decode("utf-8-sig", errors="replace"); break
    if not txt:
        raise RuntimeError(f"קובץ אחזקות Global X עבור {fund} לא נמצא ב-12 הימים האחרונים")
    lines = txt.splitlines()
    as_of = lines[1].replace("Fund Holdings Data as of", "").strip()
    df = pd.read_csv(io.StringIO("\n".join(lines[2:])), dtype=str, on_bad_lines="skip")
    df = df[df["Name"].notna() & df["% of Net Assets"].notna()].copy()
    df["weight"] = pd.to_numeric(df["% of Net Assets"].str.replace(",", ""), errors="coerce")
    df = df[df["weight"].notna()]
    # official sector breakdown from the fund page
    sectors, sec_asof = None, None
    try:
        h = http_get(page).text
        for a in re.findall(r'\\"data\\":\[(\{\\"label\\".*?)\]', h):
            pairs = re.findall(r'\\"label\\":\\"([^\\]+)\\",\\"value\\":([\d.\-]+)', a)
            if pairs and sum(1 for p, _ in pairs if p in SECTOR_HE or p == "Communication Services") >= max(2, len(pairs) // 2):
                sectors = [(p, float(v)) for p, v in pairs]; break
        m = re.search(r'Exposure</h3><p[^>]*>As of(?:<!-- -->| )*([A-Z][a-z]{2} \d{1,2}, \d{4})', h)
        sec_asof = m.group(1) if m else None
    except Exception as e:
        log("  globalx page failed", e)
    out = {"provider": "Global X", "fund": fund, "fund_name": lines[0].strip().strip('"'), "as_of": as_of, "url": csv_url,
           "page": page, "df": df, "sectors": sectors, "sectors_as_of": sec_asof}
    _FUND_CACHE[fund] = out
    return out

def globalx_members(f, info):
    rows = []
    for _, r in f["df"].iterrows():
        t = r.get("Ticker")
        if not isinstance(t, str) or not t.strip():
            continue  # cash / payables
        parts = t.strip().split(" ")
        if len(parts) == 2 and parts[1] in BBG_SUFFIX:
            suf, ccy = BBG_SUFFIX[parts[1]]
            y = parts[0].replace(".", "-").replace("/", "-") + suf
        elif len(parts) == 1:
            y, ccy = parts[0].replace(".", "-"), "USD"
        else:
            y, ccy = None, None
        y = ALIAS.get(y, y) if y else y
        rows.append({"ticker": t, "name": r["Name"], "weight": r["weight"], "yahoo": y, "ccy": ccy})
    m = pd.DataFrame(rows)
    ysec = {}
    for y in m["yahoo"].dropna():
        ysec[y] = YAHOO_TO_GICS.get((info.get(y) or {}).get("sector"), "Unclassified")
    m["sector"] = m["yahoo"].map(lambda y: ysec.get(y, "Unclassified"))
    m["location"] = None
    return m

# ================================================================ Yahoo
def download_closes(tickers, period="max"):
    tickers = sorted(set(t for t in tickers if t))
    out = {}
    for attempt in range(3):
        missing = [t for t in tickers if t not in out]
        if not missing:
            break
        for i in range(0, len(missing), 150):
            chunk = missing[i:i + 150]
            try:
                d = yf.download(chunk, period=period, auto_adjust=True, progress=False, group_by="column", threads=True)
                if d is None or d.empty:
                    continue
                close = d["Close"] if isinstance(d.columns, pd.MultiIndex) else d[["Close"]].rename(columns={"Close": chunk[0]})
                for t in chunk:
                    if t in close and close[t].dropna().size > 20:
                        s = close[t].dropna()
                        out[t] = s[s > 0]
            except Exception as e:
                log("  download error", e)
        if attempt == 0 and len(out) == len(tickers):
            break
        time.sleep(2)
    return out

def load_info_cache():
    p = os.path.join(CACHE_DIR, "yf_info.json")
    try:
        return json.load(open(p))
    except Exception:
        return {}

def save_info_cache(c):
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump(c, open(os.path.join(CACHE_DIR, "yf_info.json"), "w"))

def get_info(tickers, cache):
    """Yahoo quoteSummary fields per ticker, cached for INFO_TTL_DAYS."""
    out = {}
    lim = (dt.datetime.now() - dt.timedelta(days=INFO_TTL_DAYS)).isoformat()
    todo = [t for t in dict.fromkeys(tickers) if t and not (t in cache and cache[t].get("_fetched", "") > lim)]
    for t in todo:
        try:
            i = yf.Ticker(t).info or {}
            cache[t] = {k: i.get(k) for k in ["marketCap", "trailingPE", "dividendYield", "currency", "sector", "longName"]}
        except Exception as e:
            cache[t] = {"error": str(e)[:100]}
        cache[t]["_fetched"] = dt.datetime.now().isoformat(timespec="seconds")
        time.sleep(0.25)
    for t in tickers:
        if t:
            out[t] = cache.get(t, {})
    return out

def ser_to_json(s, decimals=4):
    """Compact series: first date + day deltas + values."""
    s = s.dropna()
    if s.empty:
        return None
    idx = pd.DatetimeIndex(s.index).normalize()
    days = (idx - idx[0]).days.values
    dd = np.diff(days, prepend=0).tolist()
    return {"d0": idx[0].strftime("%Y-%m-%d"), "dd": [int(x) for x in dd],
            "v": [round(float(x), decimals) for x in s.values]}

DAILY_YEARS = 5   # daily points for the last DAILY_YEARS years (+1 month); weekly before that

def thin_compact(c):
    """Thin an encoded {d0,dd,v} series: keep daily points in the last DAILY_YEARS years (+1 month), and only the last
    observation of each calendar week before that. Idempotent. Values are never altered, only older points dropped."""
    if not c or not c.get("v"):
        return c
    days = np.cumsum(np.asarray(c["dd"], dtype=np.int64))
    idx = pd.Timestamp(c["d0"]) + pd.to_timedelta(days, unit="D")
    cut = idx[-1] - pd.DateOffset(years=DAILY_YEARS, months=1)
    old = np.flatnonzero(idx < cut)
    if old.size == 0:
        return c
    wk = pd.DatetimeIndex(idx[old]).to_period("W")
    keep_old = [old[i] for i in range(old.size) if i == old.size - 1 or wk[i] != wk[i + 1]]
    keep = np.concatenate([np.asarray(keep_old, dtype=np.int64), np.flatnonzero(idx >= cut)])
    kd = days[keep]
    return {"d0": (pd.Timestamp(c["d0"]) + pd.Timedelta(days=int(kd[0]))).strftime("%Y-%m-%d"),
            "dd": [int(x) for x in np.diff(kd - kd[0], prepend=0)], "v": [c["v"][i] for i in keep]}

def thin_all(o):
    if isinstance(o, dict):
        if "d0" in o and "dd" in o and "v" in o:
            return thin_compact(o)
        return {k: thin_all(v) for k, v in o.items()}
    if isinstance(o, list):
        return [thin_all(v) for v in o]
    return o

def ret_table(s):
    s = s.dropna()
    if len(s) < 2:
        return {}
    last_d, last = s.index[-1], float(s.iloc[-1])
    def chg(date):
        if s.index[0] > date:
            return None
        v = float(s[s.index <= date].iloc[-1])
        return None if v == 0 else round((last / v - 1) * 100, 2)
    res = {"last": round(last, 4), "last_date": last_d.strftime("%Y-%m-%d"),
           "1D": round((last / float(s.iloc[-2]) - 1) * 100, 2),
           "1M": chg(last_d - pd.DateOffset(months=1)), "3M": chg(last_d - pd.DateOffset(months=3)),
           "YTD": chg(pd.Timestamp(year=last_d.year, month=1, day=1) - pd.Timedelta(days=1)),
           "1Y": chg(last_d - pd.DateOffset(years=1)), "3Y": chg(last_d - pd.DateOffset(years=3)),
           "5Y": chg(last_d - pd.DateOffset(years=5)), "first_date": s.index[0].strftime("%Y-%m-%d")}
    w = s[s.index >= last_d - pd.DateOffset(years=1)]
    r1 = w.pct_change().dropna()
    res["vol1Y"] = round(float(r1.std() * math.sqrt(252) * 100), 2) if len(r1) > 50 else None
    res["maxDD1Y"] = round(float((w / w.cummax() - 1).min() * 100), 2) if len(w) > 50 else None
    return res

def to_usd(s, ccy, fx):
    if s is None:
        return None
    if ccy in (None, "USD"):
        return s
    f = fx.get(ccy)
    if f is None:
        return None
    return (s / f.reindex(s.index).ffill()).dropna()

def build_proxy(members, closes, fx, start):
    """Constant current-weight, daily-rebalanced basket in USD. members: list of (yahoo, weight, ccy)."""
    rets, W = {}, {}
    total_w = sum(w for _, w, _ in members)
    for t, w, ccy in members:
        s = closes.get(t)
        if s is None:
            continue
        s = to_usd(s[s.index >= start], ccy, fx)
        if s is None or len(s) < 20:
            continue
        r = s.pct_change()
        r[(r > 1.0) | (r < -0.8)] = np.nan  # drop obvious bad prints (>+100% / <-80% in one day)
        rets[t] = r; W[t] = w
    if not rets or total_w <= 0:
        return None, 0.0, 0
    R = pd.DataFrame(rets).sort_index()
    W = pd.Series(W)
    wmat = R.notna().mul(W, axis=1)
    cov = wmat.sum(axis=1) / total_w
    port = R.fillna(0).mul(W, axis=1).sum(axis=1) / wmat.sum(axis=1).replace(0, np.nan)
    ok = cov >= PROXY_MIN_COVERAGE
    if not ok.any():
        return None, float(cov.max()), len(W)
    first, last = ok.idxmax(), ok[::-1].idxmax()
    port = port[(port.index >= first) & (port.index <= last)].fillna(0)
    port.iloc[0] = 0
    return (1 + port).cumprod() * 100, float(cov[cov.index <= last].iloc[-1]), len(W)

def location_weight(f, location):
    df = f["df"]
    if "Location" not in df:
        return None, []
    e = df[(df["Location"] == location) & (df["Asset Class"] == "Equity")]
    top = e.sort_values("Weight (%)", ascending=False).head(5)
    return round(float(e["Weight (%)"].sum()), 2), [{"ticker": r["Ticker"], "name": r["Name"], "weight": round(float(r["Weight (%)"]), 2)} for _, r in top.iterrows()]

# ================================================================ market
def build_market(mid, cfg, do_fund, info_cache):
    log("market", mid)
    gaps, fetched_at = [], now_iso()
    D = {"id": mid, "name_he": cfg["name_he"], "flag": cfg.get("flag", ""), "region_he": cfg.get("region_he", ""),
         "currency": cfg["currency"], "generated_at": fetched_at, "index_note": cfg.get("index_note")}

    # ---- sector source
    ss = cfg.get("sector_source") or {}
    src, members = None, None
    try:
        if ss.get("provider") == "ishares":
            src = fetch_ishares(ss["fund"]); members = ishares_members(src)
        elif ss.get("provider") == "globalx":
            src = fetch_globalx(ss["fund"])
            pre = []
            for t in src["df"]["Ticker"].dropna():
                p = t.strip().split(" ")
                if len(p) == 2 and p[1] in BBG_SUFFIX: pre.append(p[0].replace(".", "-").replace("/", "-") + BBG_SUFFIX[p[1]][0])
                elif len(p) == 1 and p[0]: pre.append(p[0].replace(".", "-"))
            pre = [ALIAS.get(x, x) for x in pre]
            info_m = get_info(pre, info_cache)
            members = globalx_members(src, info_m)
    except Exception as e:
        gaps.append(f"קובץ אחזקות {ss.get('fund')} לא נטען: {e}")
    if ss.get("provider") is None:
        gaps.append("פילוח סקטורים: " + cfg.get("no_sector_reason", "אין מקור"))

    # ---- choose proxy members (top N per sector by weight)
    sel = None
    if members is not None and len(members):
        sel = (members[members["yahoo"].notna()].sort_values("weight", ascending=False)
               .groupby("sector", group_keys=False).head(PROXY_MAX_MEMBERS))
    # ---- downloads
    idx_t = (cfg.get("index") or {}).get("ticker")
    long_t = [idx_t, cfg.get("fallback"), cfg.get("extra_index")] + cfg.get("etfs", []) + list((cfg.get("sector_etfs") or {}).values())
    ccys = set([cfg["currency"]] + ([] if sel is None else list(sel["ccy"].dropna().unique())))
    if cfg.get("extra_fx"):
        ccys.add(cfg["extra_fx"].replace("=X", ""))
    fx_t = {c: f"{c}=X" for c in ccys if c and c != "USD"}
    log("  downloading index/ETF/FX")
    closes = download_closes(long_t + list(fx_t.values()), "max")
    if sel is not None:
        log("  downloading", len(sel), "constituents")
        closes.update(download_closes(list(sel["yahoo"]), f"{PROXY_YEARS}y"))
    fx = {c: closes[t] for c, t in fx_t.items() if t in closes}
    miss_long = [t for t in long_t + list(fx_t.values()) if t and t not in closes]
    if miss_long:
        gaps.append("אין היסטוריית מחירים ב-Yahoo Finance עבור: " + ", ".join(sorted(set(miss_long))))
    if sel is not None:
        miss = sel[~sel["yahoo"].isin(list(closes))]
        if len(miss):
            gaps.append(f"{len(miss)} מתוך {len(sel)} רכיבים שנבחרו לפרוקסי חסרים ב-Yahoo (משקל כולל {miss['weight'].sum():.2f}% מהקרן): "
                        + ", ".join(miss["yahoo"].head(15)) + (" …" if len(miss) > 15 else ""))

    # ---- main line
    idx = closes.get(idx_t) if idx_t else None
    if idx is not None and len(idx) > 250:
        kind = cfg.get("index_kind", "index")
        D["index"] = {"ticker": idx_t, "name_he": cfg["index"]["name_he"], "kind": kind, "currency": cfg["currency"],
                      "source": "Yahoo Finance (yfinance)", "fetched_at": fetched_at, "series": ser_to_json(idx, 2), "returns": ret_table(idx)}
        main_usd = to_usd(idx, cfg["currency"], fx)
    else:
        if idx_t:
            gaps.append(f"למדד {idx_t} אין היסטוריה שמישה ב-Yahoo — מוצגת קרן {cfg.get('fallback')} במקומו")
        fb = cfg.get("fallback"); s = closes.get(fb) if fb else None
        if s is not None:
            D["index"] = {"ticker": fb, "name_he": f"קרן סל {fb} (במקום מדד)", "kind": "etf", "currency": "USD",
                          "source": "Yahoo Finance (yfinance)", "fetched_at": fetched_at, "series": ser_to_json(s, 4), "returns": ret_table(s)}
        else:
            D["index"] = None
            gaps.append("אין מדד וגם אין קרן סל זמינה לשוק זה ב-Yahoo Finance")
        main_usd = s
    extra = []
    if idx is not None and D["index"] and D["index"]["ticker"] == idx_t and cfg["currency"] != "USD" and main_usd is not None:
        extra.append({"ticker": f"{idx_t} USD", "label": f"{cfg['index']['name_he']} בדולרים (מדד ÷ {fx_t.get(cfg['currency'])})",
                      "currency": "USD", "series": ser_to_json(main_usd, 4), "returns": ret_table(main_usd)})
    if cfg.get("extra_index") and cfg["extra_index"] in closes:
        s = closes[cfg["extra_index"]]
        extra.append({"ticker": cfg["extra_index"], "label": f"מדד {cfg['extra_index']}", "currency": cfg["currency"],
                      "series": ser_to_json(s, 2), "returns": ret_table(s)})
    for e in cfg.get("etfs", []):
        if e in closes and not (D["index"] and D["index"]["ticker"] == e):
            extra.append({"ticker": e, "label": f"קרן סל {e} (USD)", "currency": "USD", "series": ser_to_json(closes[e], 4), "returns": ret_table(closes[e])})
    D["extra_series"] = extra
    D["market_usd"] = {"label": (f"{cfg['index']['name_he']} בדולרים" if (idx is not None and cfg["currency"] != "USD" and D["index"] and D["index"]["ticker"] == idx_t)
                                 else (D["index"]["name_he"] if D["index"] else None)),
                       "ticker": D["index"]["ticker"] if D["index"] else None,
                       "series": ser_to_json(main_usd, 4) if main_usd is not None else None}
    fxs = []
    for c in [cfg["currency"], (cfg.get("extra_fx") or "").replace("=X", "")]:
        if c and c != "USD" and c in fx:
            fxs.append({"ticker": fx_t[c], "label": f"{c} ל-1 דולר", "series": ser_to_json(fx[c], 4), "returns": ret_table(fx[c])})
    D["fx"] = fxs

    # ---- fundamentals
    fund_info, fund_src = {}, None
    if do_fund and sel is not None:
        tops = list(sel.groupby("sector", group_keys=False).head(TOP_N)["yahoo"])
        log("  fundamentals for", len(tops))
        fund_info = get_info(tops, info_cache)
        fund_src = f"Yahoo Finance quote summary (yfinance .info), נמשך עד {INFO_TTL_DAYS} ימים לפני {fetched_at[:10]}"
    elif not do_fund:
        gaps.append("שווי שוק / מכפיל / דיבידנד לא נמשכו בריצה זו (--no-fundamentals)")

    # ---- sectors
    D["sectors"] = None
    if src is not None and members is not None:
        if src["provider"] == "iShares":
            st = src["df"].groupby("Sector")["Weight (%)"].agg(["sum", "count"]).sort_values("sum", ascending=False)
            weights = [(s, float(r["sum"])) for s, r in st.iterrows()]
            basis = "משקל בקרן לפי שווי שוק (Weight %) כפי שמפרסמת iShares בקובץ האחזקות; סיווג GICS"
            w_asof, w_url = src["as_of"], src["url"]
            member_note = None
        else:
            if src.get("sectors"):
                weights = src["sectors"]; w_asof = src.get("sectors_as_of") or "לא צוין"; w_url = src["page"]
                basis = "פילוח סקטורים (GICS) כפי שמפורסם בעמוד הקרן של Global X"
            else:
                g = members.groupby("sector")["weight"].sum().sort_values(ascending=False)
                weights = [(s, float(v)) for s, v in g.items()]; w_asof = src["as_of"]; w_url = src["url"]
                basis = "סכום משקלי האחזקות לפי סיווג הסקטורים של Yahoo Finance (פילוח רשמי לא נמצא בעמוד הקרן)"
                gaps.append(f"פילוח הסקטורים הרשמי של {src['fund']} לא נקרא מעמוד הקרן — חושב מסיווג Yahoo")
            member_note = (f"שיוך כל מניה לסקטור (לרשימות המניות ולפרוקסי) לפי סיווג Yahoo Finance; משקלי המניות מקובץ האחזקות "
                           f"של {src['fund']} נכון ל-{src['as_of']}. ייתכנו הבדלים קלים מול הפילוח הרשמי.")
        start = pd.Timestamp.today().normalize() - pd.DateOffset(years=PROXY_YEARS)
        # regional
        regional = {}
        for rf in cfg.get("regional", []):
            try:
                regional[rf] = fetch_ishares(rf)
            except Exception as e:
                gaps.append(f"קרן אזורית {rf} לא נטענה: {e}")
        items = []
        for sec, w in weights:
            gsec = "Communication" if sec == "Communication Services" and src["provider"] == "iShares" else sec
            ent = {"sector": sec, "sector_he": SECTOR_HE.get(sec, sec), "weight": round(w, 2),
                   "is_equity": sec not in ("Cash and/or Derivatives", "Other", "Unclassified")}
            msec = members[members["sector"].isin([sec, "Communication" if sec == "Communication Services" else sec,
                                                   "Communication Services" if sec == "Communication" else sec])].sort_values("weight", ascending=False)
            ent["n"] = int(len(msec))
            if ent["is_equity"]:
                tot = msec["weight"].sum()
                ent["top3_share_of_sector"] = round(float(msec["weight"].head(3).sum() / tot * 100), 1) if tot > 0 else None
                tops = []
                for _, r in msec.head(TOP_N).iterrows():
                    fi = fund_info.get(r["yahoo"], {}) if r["yahoo"] else {}
                    s = closes.get(r["yahoo"]) if r["yahoo"] else None
                    tops.append({"ticker": r["ticker"], "yahoo": r["yahoo"], "name": r["name"], "weight": round(float(r["weight"]), 2),
                                 "marketCap": fi.get("marketCap"), "mcap_ccy": fi.get("currency"),
                                 "pe": round(fi["trailingPE"], 1) if isinstance(fi.get("trailingPE"), (int, float)) else None,
                                 "divYield": fi.get("dividendYield") if isinstance(fi.get("dividendYield"), (int, float)) else None,
                                 "ret1Y": ret_table(s).get("1Y") if s is not None else None})
                ent["top"] = tops
                # sector chart: real sector ETF if configured, else constructed proxy (USD)
                setf = (cfg.get("sector_etfs") or {}).get(gsec) or (cfg.get("sector_etfs") or {}).get(sec)
                if setf and setf in closes:
                    s = closes[setf]
                    ent["chart"] = {"kind": "etf", "ticker": setf, "label": f"קרן סקטור {setf} — {SECTOR_ETF_NAMES.get(setf, '')}",
                                    "method": f"מחירי סגירה מותאמים של {setf} מ-Yahoo Finance (USD).", "currency": "USD",
                                    "series": ser_to_json(s[s.index >= start], 3), "returns": ret_table(s)}
                else:
                    mem = [(r["yahoo"], float(r["weight"]), r["ccy"]) for _, r in msec.head(PROXY_MAX_MEMBERS).iterrows() if r["yahoo"]]
                    lvl, cov, n_used = build_proxy(mem, closes, fx, start)
                    if lvl is not None:
                        ent["chart"] = {"kind": "proxy", "label": f"פרוקסי מחושב: סל מניות {ent['sector_he']} מתוך {src['fund']}",
                                        "method": (f"תשואה יומית משוקללת לפי משקלי {src['fund']} נכון ל-{src['as_of']} (משקל קבוע, איזון יומי), עד {PROXY_MAX_MEMBERS} המניות הגדולות בסקטור, "
                                                   f"מחירי סגירה מותאמים מ-Yahoo מומרים לדולר. אינו מדד רשמי; ההרכב הנוכחי מוחל על העבר (הטיית שורדים)."),
                                        "coverage_last": round(cov * 100, 1), "n_used": n_used, "currency": "USD",
                                        "series": ser_to_json(lvl, 2), "returns": ret_table(lvl)}
                    elif not mem:
                        ent["chart"] = None
                        gaps.append(f"סקטור {SECTOR_HE.get(sec, sec)}: אין בקובץ האחזקות מניות המשויכות לסקטור זה לפי הסיווג בשימוש — אין רשימת מניות וגרף (המשקל עצמו מהפילוח הרשמי)")
                    else:
                        ent["chart"] = None
                        gaps.append(f"גרף לסקטור {SECTOR_HE.get(sec, sec)} לא נבנה — אין מחירים למספיק רכיבים (כיסוי {cov*100:.0f}%)")
            # same sector inside regional funds
            ent["other_funds"] = []
            loc = ss.get("location")
            for rf, f in regional.items():
                if loc and "Location" in f["df"]:
                    sub = f["df"][(f["df"]["Location"] == loc) & (f["df"]["Sector"] == gsec)]
                    ent["other_funds"].append({"fund": rf, "name": f["fund_name"], "as_of": f["as_of"], "weight": round(float(sub["Weight (%)"].sum()), 2)})
            items.append(ent)
        D["sectors"] = {"provider": src["provider"], "fund": src["fund"], "fund_name": src["fund_name"], "as_of": w_asof,
                        "holdings_as_of": src["as_of"], "url": w_url, "holdings_url": src["url"], "page": src["page"],
                        "fetched_at": fetched_at, "basis": basis, "member_note": member_note,
                        "fundamentals_source": fund_src, "items": items}
        exp = []
        for rf, f in regional.items():
            if ss.get("location"):
                w, top = location_weight(f, ss["location"])
                if not w:
                    gaps.append(f"{rf} ({f['fund_name']}): אין בקרן מניות עם Location = {ss['location']} (נכון ל-{f['as_of']})")
                    continue
                exp.append({"fund": rf, "name": f["fund_name"], "as_of": f["as_of"], "page": f["page"], "country_weight": w, "top": top})
        D["fund_exposure"] = exp
    else:
        D["fund_exposure"] = []
    D["gaps"] = gaps
    return D

# ================================================================ output
def js_dump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

def clean_nan(o):
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: clean_nan(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean_nan(v) for v in o]
    return o

def data_path(name):
    return os.path.join(ROOT, "data", name)

def load_previous(mid):
    try:
        return json.load(open(data_path(f"{mid}.json"), encoding="utf-8"))
    except Exception:
        return None

def keep_previous_parts(mid, D):
    """Never replace good data with nothing because one source failed in this run."""
    prev = load_previous(mid)
    if not prev:
        return D
    if not D.get("index") and prev.get("index"):
        log("  index missing in this run -> keeping previous file for", mid)
        prev.setdefault("gaps", []).append(f"ריצת העדכון ב-{D['generated_at'][:16]} לא הצליחה למשוך מחירים — מוצגים נתוני הריצה הקודמת")
        return prev
    if not D.get("sectors") and prev.get("sectors"):
        log("  sectors missing in this run -> keeping previous sector data for", mid)
        D["sectors"] = prev["sectors"]; D["fund_exposure"] = prev.get("fund_exposure", [])
        D["gaps"].append(f"פילוח הסקטורים לא נמשך בריצה זו — מוצג הפילוח מהריצה הקודמת (נכון ל-{prev['sectors'].get('as_of')}, נמשך {str(prev['sectors'].get('fetched_at'))[:10]})")
    return D

def write_outputs(mid, D):
    js = js_dump(thin_all(clean_nan(D)))
    open(data_path(f"{mid}.json"), "w", encoding="utf-8").write(js)

def write_index():
    avail = []
    for mid, cfg in MARKETS.items():
        D = load_previous(mid)
        if D:
            avail.append({"id": mid, "name_he": cfg["name_he"], "flag": cfg.get("flag", ""), "region_he": cfg.get("region_he", ""),
                          "updated": D.get("generated_at"), "bytes": os.path.getsize(data_path(f"{mid}.json"))})
    open(data_path("markets.json"), "w", encoding="utf-8").write(js_dump(avail))
    return [a["id"] for a in avail]

def build_standalone(ids, out_name):
    """Optional single-file build (everything inlined). Large; meant for desktop/offline use, not deployed."""
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "assets", "style.css"), encoding="utf-8").read()
    app = open(os.path.join(ROOT, "assets", "app.js"), encoding="utf-8").read()
    plotly = open(os.path.join(ROOT, "assets", "plotly-basic.min.js"), encoding="utf-8").read()
    assert "</script" not in plotly.lower()
    mk = json.load(open(data_path("markets.json"), encoding="utf-8"))
    data = {m: json.load(open(data_path(f"{m}.json"), encoding="utf-8")) for m in ids}
    boot = ("window.STANDALONE=" + json.dumps(ids) + ";window.MARKETS=" + js_dump(mk) + ";window.MARKET_DATA=" + js_dump(data) + ";").replace("</", "<\\/")
    html = re.sub(r'<link rel="(manifest|apple-touch-icon|icon)"[^>]*>\n?', "", html)
    html = html.replace('<link rel="stylesheet" href="assets/style.css">', "<style>\n" + css + "\n</style>")
    html = html.replace('<script defer src="assets/plotly-basic.min.js"></script>', "<script>" + plotly + "</script>")
    html = html.replace('<script defer src="assets/app.js"></script>', "<script>" + boot + "</script><script>\n" + app + "\n</script>")
    open(os.path.join(ROOT, out_name), "w", encoding="utf-8").write(html)
    log("standalone", out_name, f"{os.path.getsize(os.path.join(ROOT, out_name))/1e6:.1f} MB")

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ids = args or list(MARKETS)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    failures = 0
    if "--build-only" in sys.argv:
        for mid in ids:
            D = load_previous(mid)
            if D:
                write_outputs(mid, D)
    else:
        do_fund = "--no-fundamentals" not in sys.argv
        cache = load_info_cache()
        for mid in ids:
            if mid not in MARKETS:
                log("unknown market", mid); continue
            try:
                D = keep_previous_parts(mid, build_market(mid, MARKETS[mid], do_fund, cache))
                write_outputs(mid, D)
                log("  wrote", mid, "gaps:", len(D["gaps"]), f"{os.path.getsize(data_path(mid + '.json'))/1024:.0f} KB")
            except Exception:
                failures += 1
                traceback.print_exc()
            save_info_cache(cache)
    avail = write_index()
    log("markets with data:", len(avail))
    if "--standalone" in sys.argv:
        build_standalone(avail, "markets_standalone.html")
    if "--per-market-standalone" in sys.argv:
        for m in avail:
            build_standalone([m], f"{m}_standalone.html")
    return 1 if failures and failures == len(ids) else 0

if __name__ == "__main__":
    sys.exit(main())
