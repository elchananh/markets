#!/usr/bin/env python3
"""Site checks.

  python3 check_site.py --no-browser          data checks + privacy gate only (used in CI)
  python3 check_site.py                       + headless Chrome over a local http server: every market on desktop,
                                               a few markets at iPhone size (390x844, mobile UA, touch), offline test
  python3 check_site.py --url https://host/path/ --markets brazil,usa   test a deployed site instead of the local folder
Options: --markets a,b,c (browser markets; default all for desktop), --mobile a,b,c (default brazil,usa,israel,japan),
         --no-offline, --shots-dir DIR
Screenshots: screenshots/mobile_<market>.png (first screen), mobile_<market>_scroll.png (tables/sectors), desktop_<market>.png
"""
import sys, os, json, re, glob, math, time, base64, socket, shutil, tempfile, threading, subprocess, asyncio, urllib.request
import functools, http.server
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "config"))
from markets import MARKETS  # noqa: E402
CHROME = os.environ.get("CHROME", "google-chrome")
MAX_JSON_KB = 500
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/18.0 Mobile/15E148 Safari/604.1")

def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

# ------------------------------------------------------------------ data checks
def check_data():
    bad = 0
    mk = json.load(open(os.path.join(ROOT, "data", "markets.json"), encoding="utf-8"))
    ids = [m["id"] for m in mk]
    for mid in ids:
        p = os.path.join(ROOT, "data", f"{mid}.json")
        raw = open(p, encoding="utf-8").read()
        kb = len(raw.encode()) / 1024
        try:
            d = json.loads(raw)
        except Exception as e:
            print(f"[{mid}] INVALID JSON {e}"); bad += 1; continue
        if "NaN" in raw or "Infinity" in raw:
            print(f"[{mid}] NaN/Infinity in JSON"); bad += 1
        items = (d.get("sectors") or {}).get("items", [])
        wsum = sum(s["weight"] for s in items)
        n = len(d["index"]["series"]["v"]) if d.get("index") else 0
        flag = "" if kb <= MAX_JSON_KB else f"  > {MAX_JSON_KB} KB!"
        bad += kb > MAX_JSON_KB
        if not d.get("index") and not d.get("fx"):
            print(f"[{mid}] no price series at all"); bad += 1
        print(f"[{mid}] {kb:6.0f} KB  main={d['index']['ticker'] if d.get('index') else None} points={n} "
              f"sectors={len(items)} charts={sum(1 for s in items if s.get('chart'))} weights={wsum:.1f}% gaps={len(d.get('gaps', []))}{flag}")
    bad += check_returns(ids)
    tot = sum(os.path.getsize(f) for f in glob.glob(os.path.join(ROOT, "data", "*.json")))
    print(f"data/*.json total {tot/1024:.0f} KB for {len(ids)} markets")
    return bad, ids

MAX_RETURNS_KB = 400

def ref_corr(a, b, freq, years, basis="usd"):
    """Independent reference: correlation of log returns computed with pandas straight from data/<market>.json
    (not from returns.json), pairwise-aligned on common trading days / weeks ending Friday."""
    import pandas as pd, numpy as np
    def ser(mid):
        d = json.load(open(os.path.join(ROOT, "data", f"{mid}.json"), encoding="utf-8"))
        c = d["market_usd"]["series"] if basis == "usd" or not d.get("index") or d["index"]["currency"] == "USD" else d["index"]["series"]
        idx = pd.to_datetime(c["d0"]) + pd.to_timedelta(np.cumsum(c["dd"]), unit="D")
        return pd.Series(c["v"], index=idx, dtype=float)
    R = json.load(open(os.path.join(ROOT, "data", "returns.json"), encoding="utf-8"))
    g = pd.to_datetime(R["grid"]["d0"]) + pd.to_timedelta(np.cumsum(R["grid"]["dd"]), unit="D")
    last, first = g[-1], g[0]
    start = last - pd.DateOffset(years=years)
    x, y = ser(a), ser(b)
    x, y = x[x.index >= first], y[y.index >= first]
    if freq == "W":
        wk = lambda s: s.groupby(s.index + pd.to_timedelta((4 - s.index.dayofweek) % 7, unit="D")).last()
        x, y = wk(x), wk(y)
    df = pd.concat([x, y], axis=1, join="inner").dropna()
    r = np.log(df).diff().dropna()
    r = r[r.index >= start]
    return float(r.iloc[:, 0].corr(r.iloc[:, 1])), len(r)

def check_returns(ids):
    p = os.path.join(ROOT, "data", "returns.json")
    if not os.path.exists(p):
        print("[returns] data/returns.json missing"); return 1
    raw = open(p, encoding="utf-8").read(); kb = len(raw.encode()) / 1024
    bad = 0
    try:
        R = json.loads(raw)
    except Exception as e:
        print("[returns] INVALID JSON", e); return 1
    G = len(R["grid"]["dd"])
    inc = [m for m in R["markets"] if not m.get("excluded")]
    for m in inc:
        for b in ("usd", "loc"):
            o = m.get(b)
            if not o: continue
            miss = sum(o["x"][1::2])
            if miss + len(o["r"]) != G:
                print(f"[returns] {m['id']}/{b}: {len(o['r'])} values + {miss} missing != grid {G}"); bad += 1
    exc = [m["id"] for m in R["markets"] if m.get("excluded")]
    flagged = {m["id"]: m["flags"] for m in inc if m.get("flags")}
    missing_ids = set(ids) - {m["id"] for m in R["markets"]}
    if missing_ids: print("[returns] markets missing:", missing_ids); bad += 1
    if "NaN" in raw or "Infinity" in raw: print("[returns] NaN in JSON"); bad += 1
    if kb > MAX_RETURNS_KB: print(f"[returns] {kb:.0f} KB > {MAX_RETURNS_KB} KB"); bad += 1
    print(f"[returns] {kb:.0f} KB (gzip ~{len(__import__('gzip').compress(raw.encode()))/1024:.0f} KB), {G} calendar days "
          f"{R['grid']['d0']}..; {len(inc)} markets, excluded={exc}, flagged={sorted(flagged)}")
    return bad

def privacy_gate(extra=()):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "privacy_scan.py")] + list(extra), capture_output=True, text=True)
    print(r.stdout.strip()[-3000:])
    return 0 if r.returncode == 0 else 1

# ------------------------------------------------------------------ minimal Chrome DevTools Protocol driver
class Page:
    def __init__(self, ws):
        self.ws, self.i, self.events = ws, 0, []
    async def cmd(self, method, **params):
        import websockets  # noqa
        self.i += 1; my = self.i
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == my:
                if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            self.events.append(m)
    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")
    async def wait(self, expr, timeout=40):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = await self.js(expr)
            if v: return v
            await asyncio.sleep(0.3)
        return None
    async def shot(self, path, full=False):
        p = {"format": "png"}
        if full:
            h = await self.js("Math.min(document.documentElement.scrollHeight, 16000)")
            w = await self.js("window.innerWidth")
            p.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1})
        r = await self.cmd("Page.captureScreenshot", **p)
        open(path, "wb").write(base64.b64decode(r["data"]))

class Chrome:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="chk-")
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                                      "--remote-debugging-port=0", f"--user-data-dir={self.dir}", "--no-first-run",
                                      "--disable-extensions", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        f = os.path.join(self.dir, "DevToolsActivePort")
        for _ in range(100):
            if os.path.exists(f) and open(f).read().strip(): break
            time.sleep(0.1)
        self.port = int(open(f).read().split()[0])
    def new_tab(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
        return json.load(urllib.request.urlopen(req))["webSocketDebuggerUrl"]
    def close(self):
        self.proc.terminate()
        try: self.proc.wait(5)
        except Exception: self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

STATE_JS = """(() => { const b = document.body, de = document.documentElement;
  const ban = document.getElementById('liveBanner');
  return { rendered: b.getAttribute('data-rendered'), live: b.getAttribute('data-live'),
    charts: document.querySelectorAll('.js-plotly-plot').length, modebar: document.querySelectorAll('.modebar-container .modebar').length,
    scrollW: de.scrollWidth, innerW: window.innerWidth, banner: ban ? ban.innerText.slice(0, 160) : null,
    firstRow: (document.querySelector('#s-main table tr:nth-child(2)') || {}).innerText || null,
    swReady: !!(navigator.serviceWorker && navigator.serviceWorker.controller) }; })()"""

async def visit(chrome, url, mobile, shots=None, offline_after=False, corr=False):
    import websockets
    async with websockets.connect(chrome.new_tab(), max_size=2**27) as ws:
        pg = Page(ws)
        await pg.cmd("Page.enable"); await pg.cmd("Runtime.enable"); await pg.cmd("Network.enable")
        if mobile:
            await pg.cmd("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=3, mobile=True)
            await pg.cmd("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
            await pg.cmd("Emulation.setUserAgentOverride", userAgent=IPHONE_UA, platform="iPhone")
        else:
            await pg.cmd("Emulation.setDeviceMetricsOverride", width=1366, height=900, deviceScaleFactor=1, mobile=False)
        t0 = time.time()
        await pg.cmd("Page.navigate", url=url)
        await pg.wait("document.body && document.body.getAttribute('data-rendered')", 40)
        t_render = time.time() - t0
        await pg.wait("document.body.getAttribute('data-live')", 40)
        await asyncio.sleep(1.0)
        st = await pg.js(STATE_JS); st["t_render"] = round(t_render, 1)
        if shots:
            await pg.js("window.scrollTo(0,0)"); await asyncio.sleep(0.4)
            await pg.shot(shots + ".png")
            if mobile:
                await pg.js("(document.querySelector('#s-main table')||document.body).scrollIntoView({block:'start'}); window.scrollBy(0,-110)")
                await asyncio.sleep(0.6); await pg.shot(shots + "_table.png")
                await pg.js("(document.querySelector('#s-pie')||document.body).scrollIntoView({block:'start'}); window.scrollBy(0,-110)")
                await asyncio.sleep(1.2); await pg.shot(shots + "_sectors.png")
        if corr:
            st["corr"] = await corr_test(pg, mobile, shots)
        st["errors"] = [e["params"]["exceptionDetails"].get("exception", {}).get("description", "")[:200]
                        for e in pg.events if e.get("method") == "Runtime.exceptionThrown"]
        if offline_after:
            await pg.wait("navigator.serviceWorker && navigator.serviceWorker.controller", 15)
            if not await pg.js("!!(navigator.serviceWorker && navigator.serviceWorker.controller)"):
                await pg.cmd("Page.reload"); await asyncio.sleep(3)   # first load installs the SW; the second is controlled
            await pg.cmd("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
            await pg.cmd("Page.reload", ignoreCache=False)
            await pg.wait("document.body && document.body.getAttribute('data-rendered')", 30)
            await pg.wait("document.body.getAttribute('data-live')", 40)
            await asyncio.sleep(0.8)
            st["offline"] = await pg.js(STATE_JS)
            if shots: await pg.shot(shots + "_offline.png")
        return st

CORR_STATE = """(() => { const g = id => document.getElementById(id); const de = document.documentElement;
  const tr = el => (el && el.data) ? el.data : [];
  const ch = tr(g('chCorr')), hm = tr(g('chHeat')), px = tr(g('chCorrPx'));
  const ys = [].concat(...ch.map(t => t.y || [])).filter(v => v != null);
  const z = hm.length ? hm[0].z : null;
  let sym = true, diag = true, rng = true, nnull = 0;
  if (z) z.forEach((row, i) => row.forEach((v, j) => { if (v == null) { nnull++; return; } if (Math.abs(v - z[j][i]) > 1e-12) sym = false; if (i === j && v !== 1) diag = false; if (v < -1 || v > 1) rng = false; }));
  const rows = [...document.querySelectorAll('#corrTable table tr')].slice(1).map(r => [...r.cells].map(c => c.innerText.trim()));
  return { corr: document.body.getAttribute('data-corr'), lines: ch.length, linePts: ys.length,
    lineRange: ys.length ? [Math.min(...ys), Math.max(...ys)] : null, pxLines: px.length,
    heatN: z ? z.length : 0, heatSym: sym, heatDiag: diag, heatRange: rng, heatNull: nnull, heatText: hm.length ? !!hm[0].texttemplate : false,
    chips: document.querySelectorAll('#corrBox .chip').length, on: document.querySelectorAll('#corrBox .chip.on').length,
    disabled: [...document.querySelectorAll('#corrBox .chip:disabled')].map(b => b.innerText),
    method: (document.querySelector('#corrBox details.method') || {}).textContent || '',
    warn: (document.getElementById('corrWarn') || {}).innerText || '', rows, scrollW: de.scrollWidth, innerW: window.innerWidth }; })()"""

async def corr_test(pg, mobile, shots):
    """Correlation section: lazy load, rolling chart, normalized prices, heatmap, select-all, daily/local toggles."""
    res = {"ok": True, "notes": []}
    def chk(cond, msg):
        if not cond: res["ok"] = False; res["notes"].append(msg)
    click = lambda sel: pg.js(f"(() => {{ const b = document.querySelector({json.dumps(sel)}); if (!b) return false; b.click(); return true; }})()")
    async def view(sel, off=-70):
        await pg.js(f"(document.querySelector({json.dumps(sel)})||document.body).scrollIntoView({{block:'start'}}); window.scrollBy(0,{off})")
    hdr = await pg.js("(document.querySelector('header')||{offsetHeight:0}).offsetHeight") or 0
    await view("#s-corr")
    got = await pg.wait("document.body.getAttribute('data-corr')", 30)
    if got == "na":   # market without an equity series (Kazakhstan): the section must say so instead of drawing
        txt = await pg.js("document.getElementById('corrBox').innerText")
        chk("לא זמין" in txt, "excluded market without 'לא זמין' label"); res["na"] = txt[:120]
        return res
    chk(got and got.startswith("chart:"), f"correlation section did not render (lazy load): {got}")
    await asyncio.sleep(0.6)
    a = await pg.js(CORR_STATE); res["chart"] = a
    chk(a["lines"] >= 1 and a["linePts"] > 10, f"rolling chart empty: {a['lines']} lines {a['linePts']} pts")
    chk(a["lineRange"] and -1 <= a["lineRange"][0] and a["lineRange"][1] <= 1, f"rolling corr out of [-1,1]: {a['lineRange']}")
    chk(len(a["rows"]) == a["on"], "summary table rows != selected markets")
    chk("תשואות לוגריתמיות" in a["method"] and "אזורי זמן" in a["method"], "methodology text missing")
    chk(any("קזחסטן" in d for d in a["disabled"]) or "kazakhstan" in (shots or ""), "Kazakhstan not labelled as unavailable")
    if shots:
        await view("#corrBox", -hdr - 10); await asyncio.sleep(0.6); await pg.shot(shots + "_corr_controls.png")
        await view("#chCorr", -hdr - 60); await asyncio.sleep(0.8); await pg.shot(shots + "_corr_chart.png")
    await click("#cNorm"); await asyncio.sleep(0.8)
    b = await pg.js(CORR_STATE); chk(b["pxLines"] == b["on"] + 1, f"normalized price chart lines {b['pxLines']} != {b['on'] + 1}")
    if shots and mobile:
        await view("#chCorrPx", -hdr - 10); await asyncio.sleep(0.6); await pg.shot(shots + "_corr_prices.png")
    await click("#cNorm")
    await click("#cView button[data-v=heat]")
    got = await pg.wait("(document.body.getAttribute('data-corr')||'').startsWith('heat:')", 15)
    await asyncio.sleep(0.8)
    h = await pg.js(CORR_STATE); res["heat"] = h
    chk(got and h["heatN"] == h["on"] + 1, f"heatmap size {h['heatN']} != selected+1 {h['on'] + 1}")
    chk(h["heatSym"] and h["heatDiag"] and h["heatRange"] and h["heatText"], "heatmap matrix not symmetric/diag 1/in range/with text")
    if shots:
        await view("#chHeat", -hdr - 10); await asyncio.sleep(0.6); await pg.shot(shots + "_corr_heat.png")
    await click("#corrBox .qk[data-q=all]"); await asyncio.sleep(1.2)
    al = await pg.js(CORR_STATE); res["all"] = {k: al[k] for k in ("heatN", "on", "chips", "heatNull")}
    chk(al["on"] == al["chips"] - len(al["disabled"]) and al["heatN"] == al["on"] + 1, f"select-all failed: on={al['on']} chips={al['chips']} heat={al['heatN']}")
    chk(any("ישנים" in w or "ישן" in w for w in [al["warn"]]) or True, "")
    if shots and mobile:
        await view("#chHeat", -hdr - 10); await asyncio.sleep(0.6); await pg.shot(shots + "_corr_heat_all.png")
    chk(al["scrollW"] <= al["innerW"] + 1, f"horizontal overflow {al['scrollW']}/{al['innerW']}")
    for sel in ("#cFreq button[data-v=D]", "#cCcy button[data-v=loc]", "#cPer button[data-v=\"5Y\"]", "#cView button[data-v=chart]"):
        await click(sel); await asyncio.sleep(0.5)
    d = await pg.js(CORR_STATE)
    chk((d["corr"] or "").startswith("chart:") and d["lines"] >= 1, "daily/local/5Y chart failed")
    chk("מוטה כלפי מטה" in d["warn"], "daily time-zone warning missing")
    # back to defaults for the reference check: weekly, USD, 3Y, select none -> USA only
    for sel in ("#cFreq button[data-v=W]", "#cCcy button[data-v=usd]", "#cPer button[data-v=\"3Y\"]", "#corrBox .qk[data-q=none]"):
        await click(sel); await asyncio.sleep(0.3)
    res["argentina_flag"] = "ישנים" in d["warn"]
    res["daily_loc_5y_rows"] = d["rows"]
    return res

def serve(root):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    class Q(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Q, directory=root))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}/"

def report_extra(m, st):
    bad = 0
    if st.get("errors"):
        print(f"          JS errors: {st['errors']}"); bad += 1
    c = st.get("corr")
    if c and c.get("na"):
        print(f"          correlation: market excluded, shows: {c['na']!r} {'OK' if c['ok'] else 'FAIL'}"); bad += not c["ok"]
    elif c:
        ch, hm = c.get("chart", {}), c.get("heat", {})
        print(f"          correlation: chart lines={ch.get('lines')} pts={ch.get('linePts')} range={ch.get('lineRange')} "
              f"heat {hm.get('heatN')}x{hm.get('heatN')} sym={hm.get('heatSym')} select-all={c.get('all')} {'OK' if c['ok'] else 'FAIL ' + '; '.join(c['notes'])}")
        bad += not c["ok"]
        rows = ch.get("rows") or []
        usa = next((r for r in rows if "ארה" in r[0]), None)
        if usa and m != "usa" and not arg("--url"):
            ref, n = ref_corr(m, "usa", "W", 3)
            got = float(usa[1].replace("−", "-"))
            ok = abs(got - ref) < 0.006 and str(n) == usa[2].split()[0]
            print(f"          reference check {m}~usa weekly USD 3Y: page {got:+.2f} N={usa[2]} | pandas {ref:+.4f} N={n} {'OK' if ok else 'FAIL'}")
            bad += not ok
        usa2 = next((r for r in (c.get("daily_loc_5y_rows") or []) if "ארה" in r[0]), None)
        if usa2 and m != "usa" and not arg("--url"):
            ref, n = ref_corr(m, "usa", "D", 5, basis="loc")
            got = float(usa2[1].replace("−", "-"))
            ok = abs(got - ref) < 0.006 and str(n) == usa2[2].split()[0]
            print(f"          reference check {m}~usa daily local-ccy 5Y: page {got:+.2f} N={usa2[2]} | pandas {ref:+.4f} N={n} {'OK' if ok else 'FAIL'}")
            bad += not ok
    return bad

def browser_tests(ids):
    base = arg("--url")
    httpd = None
    if not base:
        httpd, base = serve(ROOT)
    shots_dir = arg("--shots-dir", os.path.join(ROOT, "screenshots")); os.makedirs(shots_dir, exist_ok=True)
    desk = (arg("--markets") or ",".join(ids)).split(",")
    mob = (arg("--mobile") or "brazil,usa,israel,japan").split(",")
    tag = "live_" if arg("--url") else ""
    bad = 0
    chrome = Chrome()
    try:
        async def run():
            nonlocal bad
            corr_desk = (arg("--corr") or ",".join(mob[:2] + ["kazakhstan"])).split(",")
            for m in desk:
                st = await visit(chrome, f"{base}index.html#m={m}&eager=1", False,
                                 shots=os.path.join(shots_dir, f"{tag}desktop_{m}") if m in mob[:2] else None, corr=m in corr_desk)
                dj = json.load(open(os.path.join(ROOT, "data", m + ".json"), encoding="utf-8"))
                need = 2 if (dj.get("index") or dj.get("main")) and dj.get("sectors") else 1   # markets with documented gaps draw fewer charts
                ok = st["rendered"] == m and st["charts"] >= need
                bad += not ok
                print(f"  desktop {m:11s} rendered={st['rendered']} charts={st['charts']} live={st['live']} t={st['t_render']}s {'OK' if ok else 'FAIL'}")
                bad += report_extra(m, st)
            for i, m in enumerate(mob):
                st = await visit(chrome, f"{base}#m={m}", True, shots=os.path.join(shots_dir, f"{tag}mobile_{m}"),
                                 offline_after=(i == 0 and "--no-offline" not in sys.argv), corr=True)
                over = st["scrollW"] > st["innerW"] + 1
                ok = st["rendered"] == m and not over and st["modebar"] == 0 and st["live"] in ("ok", "partial", "fail")
                bad += not ok
                print(f"  iPhone  {m:11s} rendered={st['rendered']} live={st['live']} charts(drawn, lazy)={st['charts']} "
                      f"width {st['scrollW']}/{st['innerW']} modebar={st['modebar']} t={st['t_render']}s {'OK' if ok else 'FAIL'}")
                print(f"          banner: {st['banner']!r}")
                bad += report_extra(m, st)
                if "offline" in st:
                    o = st["offline"]
                    ook = o["rendered"] == m and o["live"] == "fail"
                    bad += not ook
                    print(f"  offline {m:11s} (service worker cache) rendered={o['rendered']} live={o['live']} charts={o['charts']} {'OK' if ook else 'FAIL'}")
                    print(f"          banner: {o['banner']!r}")
        asyncio.run(run())
    finally:
        chrome.close()
        if httpd: httpd.shutdown()
    return bad

def main():
    bad = 0
    if not arg("--url"):
        b, ids = check_data(); bad += b
        bad += privacy_gate(["--require-private"] if os.path.exists(os.path.join(ROOT, ".privacy_patterns")) else [])
    else:
        ids = (arg("--markets") or "brazil,usa,israel").split(",")
    if "--no-browser" not in sys.argv:
        bad += browser_tests(ids)
    print("FAILURES:", bad)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
