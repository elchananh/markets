/* Market overview (general market research). Loads data/markets.json + data/<id>.json on demand (or the inlined
   window.MARKETS / window.MARKET_DATA in the optional standalone build), renders with Plotly, then refreshes prices live. */
(function () {
  "use strict";
  const NA = '<span class="na" title="הנתון לא נמצא במקור">לא זמין</span>';
  const COLORS = ["#1f6feb", "#e8590c", "#2f9e44", "#ae3ec9", "#f59f00", "#0c8599", "#d6336c", "#5c940d", "#495057", "#7048e8", "#c92a2a", "#1098ad"];
  const RANGE_HE = { "1M": "חודש", "YTD": "מתחילת השנה", "1Y": "שנה", "3Y": "3 שנים", "5Y": "5 שנים", "MAX": "מקסימום" };
  let range = "1Y", current = null;
  const $ = (s, el) => (el || document).querySelector(s);
  const escA = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // Latin tokens (tickers like ^GSPC, BRL=X, 000001.SS) are wrapped in Unicode LTR isolates so RTL text does not reorder them
  const bidi = s => String(s == null ? "" : s).replace(/\^?[A-Za-z0-9][A-Za-z0-9.\-=&\/^_:%]*(?:,? +\^?[A-Za-z0-9][A-Za-z0-9.\-=&\/^_:%]*)*/g, m => "\u2066" + m + "\u2069");
  const esc = s => escA(bidi(s));
  function fmt(x, d = 2) { return (x == null || !isFinite(x)) ? NA : Number(x).toLocaleString("he-IL", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function pct(x, d = 2, signed = true) {
    if (x == null || !isFinite(x)) return NA;
    const cls = !signed ? "" : (x > 0 ? "pos" : x < 0 ? "neg" : "");
    return `<span class="ltr ${cls}">${signed && x > 0 ? "+" : ""}${fmt(x, d)}%</span>`;
  }
  function big(x, ccy) {
    if (x == null || !isFinite(x)) return NA;
    const a = Math.abs(x); let v, u;
    if (a >= 1e12) { v = x / 1e12; u = "טריליון"; } else if (a >= 1e9) { v = x / 1e9; u = "מיליארד"; } else if (a >= 1e6) { v = x / 1e6; u = "מיליון"; } else { v = x; u = ""; }
    return `${fmt(v, 1)} ${u} ${esc(ccy || "")}`;
  }
  function dateHe(iso) { if (!iso) return NA; const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso); return isNaN(d) ? esc(iso) : d.toLocaleDateString("he-IL"); }
  function dtHe(iso) { if (!iso) return NA; const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toLocaleString("he-IL", { timeZone: "Asia/Hebron", dateStyle: "short", timeStyle: "short" }) + " (שעון ישראל)"; }

  // ---------------------------------------------------------------- series helpers
  // compact series {d0, dd:[day deltas], v:[values]} -> {d:[iso], v:[]}; decoded once and cached on the object
  function S(c) {
    if (!c) return null;
    if (c._dec) return c._dec;
    if (c.d) return c;
    const out = { d: new Array(c.v.length), v: c.v };
    const t0 = Date.UTC(+c.d0.slice(0, 4), +c.d0.slice(5, 7) - 1, +c.d0.slice(8, 10));
    let day = 0;
    for (let i = 0; i < c.v.length; i++) { day += c.dd[i]; out.d[i] = new Date(t0 + day * 864e5).toISOString().slice(0, 10); }
    Object.defineProperty(c, "_dec", { value: out, enumerable: false });
    return out;
  }
  function startDate(lastIso, r) {
    const d = new Date(lastIso + "T00:00:00Z");
    if (r === "1M") d.setUTCMonth(d.getUTCMonth() - 1);
    else if (r === "YTD") return new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
    else if (r === "1Y") d.setUTCFullYear(d.getUTCFullYear() - 1);
    else if (r === "3Y") d.setUTCFullYear(d.getUTCFullYear() - 3);
    else if (r === "5Y") d.setUTCFullYear(d.getUTCFullYear() - 5);
    else return new Date(Date.UTC(1900, 0, 1));
    return d;
  }
  function cut(series, start) {
    if (!series) return { d: [], v: [] };
    const s = typeof start === "string" ? start : start.toISOString().slice(0, 10);
    let i = 0; while (i < series.d.length && series.d[i] < s) i++;
    return { d: series.d.slice(i), v: series.v.slice(i) };
  }
  function rebase(s) { if (!s.v.length) return s; const b = s.v[0]; return { d: s.d, v: s.v.map(x => +(x / b * 100).toFixed(3)) }; }
  function lastDate(D) {
    const c = [D.index && S(D.index.series), D.market_usd && S(D.market_usd.series), D.fx && D.fx[0] && S(D.fx[0].series)].find(Boolean);
    return c ? c.d[c.d.length - 1] : new Date().toISOString().slice(0, 10);
  }
  const marketStart = D => startDate(lastDate(D), range);

  // ---------------------------------------------------------------- mobile / chart config
  const MQ = window.matchMedia ? window.matchMedia("(max-width: 700px)") : { matches: false };
  const isMobile = () => MQ.matches;
  const EAGER = /(^|[#&])eager=1(&|$)/.test(location.hash);   // draw every chart immediately (used by tests / screenshots)
  function wrapTitle(t, max) {   // Plotly titles do not wrap; break long titles into lines on narrow screens
    const words = String(t).split(" "), lines = [];
    let cur = "";
    words.forEach(w => { if (cur && (cur + " " + w).length > max) { lines.push(cur); cur = w; } else cur = cur ? cur + " " + w : w; });
    if (cur) lines.push(cur);
    return lines;
  }
  const LAYOUT = (title, ytitle) => {
    const m = isMobile();
    const lines = m ? wrapTitle(title, Math.max(26, Math.floor((window.innerWidth || 390) / 9))) : [title];
    return {
      title: { text: lines.map(bidi).join("<br>"), font: { size: m ? 12.5 : 15 }, x: 0.5, xanchor: "center", y: 0.98, yanchor: "top" },
      paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: { family: "-apple-system, Segoe UI, Arial, sans-serif", color: "#1d2433", size: m ? 11 : 12 },
      margin: m ? { l: 40, r: 8, t: 22 + 16 * lines.length, b: 28 } : { l: 60, r: 20, t: 48, b: 40 }, hovermode: "x unified",
      dragmode: m ? false : "zoom",
      xaxis: { gridcolor: "#eef0f3", linecolor: "#d0d5dd", nticks: m ? 5 : undefined, fixedrange: m },
      yaxis: { gridcolor: "#eef0f3", linecolor: "#d0d5dd", title: { text: m ? "" : (ytitle || "") }, side: "left", fixedrange: m, nticks: m ? 6 : undefined },
      legend: { orientation: "h", y: m ? -0.12 : -0.15, x: 1, xanchor: "right", font: { size: m ? 10 : 12 } },
    };
  };
  const CFG = () => ({ responsive: true, displaylogo: false, displayModeBar: isMobile() ? false : "hover", scrollZoom: false,
    modeBarButtonsToRemove: ["lasso2d", "select2d"] });
  // sector charts below the fold are drawn when scrolled near (keeps the first paint fast on phones)
  let IO = null;
  function lazy(el, fn) {
    if (EAGER || !("IntersectionObserver" in window) || el.classList.contains("js-plotly-plot")) { el._draw = null; return fn(); }
    el._draw = fn;
    if (!IO) IO = new IntersectionObserver(es => es.forEach(e => {
      if (e.isIntersecting && e.target._draw) { const f = e.target._draw; e.target._draw = null; IO.unobserve(e.target); f(); }
    }), { rootMargin: "700px 0px" });
    IO.observe(el);
  }
  const line = (s, name, color, extra) => Object.assign({ x: s.d, y: s.v, type: "scatter", mode: "lines", name: bidi(name), line: { width: 1.8, color } }, extra || {});

  function retRow(label, r, src) {
    r = r || {};
    const cells = ["1D", "1M", "3M", "YTD", "1Y", "3Y", "5Y"].map(k => `<td class="num">${pct(r[k])}</td>`).join("");
    return `<tr><td>${label}</td><td class="num">${r.last != null ? fmt(r.last, 2) : NA}</td><td>${dateHe(r.last_date)}</td>${cells}<td class="num">${pct(r.vol1Y, 1, false)}</td><td class="num">${pct(r.maxDD1Y, 1)}</td><td>${src || ""}</td></tr>`;
  }
  const RET_HEAD = `<tr><th>סדרה</th><th>אחרון</th><th>תאריך</th><th>יום</th><th>חודש</th><th>3 ח׳</th><th>YTD</th><th>שנה</th><th>3 שנים</th><th>5 שנים</th><th>תנודתיות שנתית (שנה)</th><th>ירידה מקס׳ (שנה)</th><th>עדכון</th></tr>`;
  const link = (u, t) => `<a href="${escA(u)}" target="_blank" rel="noopener">${esc(t)}</a>`;

  // ---------------------------------------------------------------- page
  function render(D) {
    current = D;
    const keepSel = $("#mainSel") ? $("#mainSel").value : null, keepNorm = $("#mainNorm") ? $("#mainNorm").checked : false;
    const ix = D.index, sec = D.sectors;
    const eqItems = sec ? sec.items.filter(s => s.is_equity) : [];
    let h = "";
    h += `<section class="card"><h1>${esc(D.flag)} ${esc(D.name_he)} <span class="muted" style="font-size:15px">· ${esc(D.region_he)}</span></h1>
      <div class="kv"><span><b>עודכן:</b> ${dtHe(D.generated_at)}</span><span><b>מטבע מקומי:</b> ${esc(D.currency)}</span><span><b>טווח מוצג:</b> <span id="rangeLbl">${RANGE_HE[range]}</span></span></div>
      <div id="liveBanner" class="live ${D._live ? D._live.cls : "pending"}">${D._live ? D._live.html : "מרענן נתוני מחירים חיים…"}</div>
      ${D.index_note ? `<p class="note">${esc(D.index_note)}</p>` : ""}
      <p class="muted" style="font-size:13px">הערת מטבע: המדד המקומי מוצג במטבע ${esc(D.currency)}; קרנות סל אמריקאיות, גרפי הסקטורים וההשוואה ביניהם מוצגים בדולר (USD), ולכן כוללים את השפעת שער החליפין. מחירי Yahoo "מותאמים" (כוללים התאמת דיבידנדים/פיצולים). הנקודה האחרונה עשויה להיות תוך-יומית.</p>
      <div class="toc"><a href="#s-main">מדד מוביל</a><a href="#s-pie">התפלגות סקטורים</a><a href="#s-sectors">סקטורים</a><a href="#s-all">כל הסקטורים יחד</a><a href="#s-src">מקורות ופערים</a></div></section>`;

    // main chart
    const L = seriesList(D);
    if (L.length) {
      h += `<section class="card" id="s-main"><h2>${!ix ? "שער החליפין (אין מדד או קרן זמינים)" : ix.kind === "etf" ? "קרן סל מייצגת (המדד לא זמין)" : "המדד המוביל"}${ix ? ": " + esc(ix.name_he) : ""}</h2>
        <div class="ctl"><label>סדרה: <select id="mainSel">${L.map(o => `<option value="${o.k}">${esc(o.label)}</option>`).join("")}</select></label>
        <label><input type="checkbox" id="mainNorm"> השוואה מנורמלת (100) של כל הסדרות</label></div>
        <div id="chMain" class="chart"></div>
        <div class="src">${ix ? `מקור: ${esc(ix.source)} · טיקר ${esc(ix.ticker)} · נמשך ${dtHe(ix.fetched_at)}${ix.kind === "etf" ? " · <b>שימו לב: זו קרן סל ולא המדד עצמו</b>" : ""}` : "מקור: Yahoo Finance (yfinance). <b>מדד ומחירי קרן לשוק זה לא זמינים</b> — ראו פערים."}</div>
        <h3 style="margin-top:14px">תשואות לפי טווח (שינוי מחיר מצטבר)</h3>
        <div class="tablewrap"><table>${RET_HEAD}${L.map(o => retRow(esc(o.label), o.r, srcTag(o.obj))).join("")}</table></div>
        <div class="src">חישוב מתוך סדרות Yahoo Finance לעיל. תנודתיות = סטיית תקן של תשואות יומיות בשנה האחרונה × √252. עליית שער ${esc(D.currency)}=X = היחלשות ה-${esc(D.currency)} מול הדולר.</div>
      </section>`;
    } else {
      h += `<section class="card" id="s-main"><h2>מדד מוביל</h2><p>${NA}</p></section>`;
    }

    // pie
    if (sec) {
      const exp = D.fund_exposure || [];
      h += `<section class="card" id="s-pie"><div class="grid2"><div><h2>התפלגות סקטורים לפי שווי שוק</h2><div id="chPie" class="chart"></div></div>
        <div><h3>חשיפה ל${esc(D.name_he)} בקרנות סל אזוריות/גלובליות</h3>${exp.length ? `<div class="tablewrap"><table><tr><th>קרן</th><th>משקל מניות ${esc(D.name_he)} בקרן</th><th>מובילות (משקל בקרן)</th><th>נכון ל</th></tr>
        ${exp.map(f => `<tr><td>${link(f.page, f.fund)} — ${esc(f.name)}</td><td class="num">${pct(f.country_weight, 2, false)}</td><td>${f.top.slice(0, 3).map(t => `${esc(t.name)} ${fmt(t.weight, 2)}%`).join(", ") || NA}</td><td>${esc(f.as_of)}</td></tr>`).join("")}</table></div>
        <div class="src">מקור: קבצי אחזקות iShares (latest-holdings.csv) — שדה Location, רכיב המניות בלבד (ללא מזומן/נגזרים).</div>` : `<p>${NA}</p>`}
        <h3>טבלת משקלים</h3><div class="tablewrap"><table><tr><th>סקטור</th><th>משקל</th><th>מס׳ מניות</th></tr>${sec.items.map(s => `<tr><td>${esc(s.sector_he)}</td><td class="num">${fmt(s.weight, 2)}%</td><td class="num">${s.is_equity ? s.n : "—"}</td></tr>`).join("")}</table></div></div></div>
        <div class="src">מקור: ${link(sec.page, `${sec.fund_name} (${sec.fund})`)} — נכון ל-${esc(sec.as_of)}; ${esc(sec.basis)}. נמשך ${dtHe(sec.fetched_at)}. הקרן היא ייצוג של השוק (מדד MSCI/Solactive/אחר של הקרן), לא כל המניות הנסחרות בבורסה.</div>
        ${sec.member_note ? `<div class="src">${esc(sec.member_note)}</div>` : ""}</section>`;
    } else {
      h += `<section class="card" id="s-pie"><h2>התפלגות סקטורים</h2><p>${NA} — ${esc((D.gaps || []).find(g => g.startsWith("פילוח")) || "אין מקור פילוח זמין")}</p></section>`;
    }

    // sectors
    h += `<section class="card" id="s-sectors"><h2>סקטורים</h2>`;
    if (!eqItems.length) h += `<p>${NA}</p>`;
    eqItems.forEach((s) => {
      const i = sec.items.indexOf(s), ch = s.chart;
      const others = (s.other_funds || []).filter(o => o.weight > 0);
      const tops = s.top || [];
      h += `<div class="sector" id="sec-${i}"><h3><span class="dot" style="background:${COLORS[i % COLORS.length]}"></span>${esc(s.sector_he)} — ${fmt(s.weight, 2)}% מהשוק (לפי ${esc(sec.fund)})</h3>
        <p><b>הרכב:</b> ${s.n} מניות בסקטור בקרן ${esc(sec.fund)}, המהוות ${fmt(s.weight, 2)}% ממשקלה.${s.top3_share_of_sector != null ? ` שלוש הגדולות מרכזות ${fmt(s.top3_share_of_sector, 1)}% מהסקטור.` : ""}</p>
        <p><b>מניות מובילות:</b> ${tops.length ? tops.map(t => `${esc(t.name)} (${esc(t.ticker)}) — ${fmt(t.weight, 2)}%`).join(" · ") : NA}</p>
        <p><b>קרנות סל עם חשיפה לסקטור:</b> ${esc(sec.fund)} — ${fmt(s.weight, 2)}% מהקרן${others.map(o => ` · ${esc(o.fund)} (${esc(o.name)}) — רכיב ${esc(D.name_he)}/${esc(s.sector_he)} = ${fmt(o.weight, 2)}% מהקרן`).join("")}${ch && ch.kind === "etf" ? ` · קרן סקטור ייעודית: ${esc(ch.ticker)}` : ""}.</p>
        <div class="tablewrap"><table><tr><th>מניה</th><th>טיקר Yahoo</th><th>משקל בקרן</th><th>שווי שוק</th><th>מכפיל רווח (12ח׳)</th><th>תשואת דיבידנד</th><th>תשואה שנה (מטבע מקומי)</th></tr>
        ${tops.map(t => `<tr><td>${esc(t.name)}</td><td class="ltr">${t.yahoo ? esc(t.yahoo) : NA}</td><td class="num">${fmt(t.weight, 2)}%</td><td class="num">${big(t.marketCap, t.mcap_ccy)}</td><td class="num">${t.pe != null ? fmt(t.pe, 1) : NA}</td><td class="num">${t.divYield != null ? fmt(t.divYield, 2) + "%" : NA}</td><td class="num">${pct(t.ret1Y)}</td></tr>`).join("")}</table></div>
        ${sec.fundamentals_source ? `<div class="src">שווי שוק / מכפיל / דיבידנד: ${esc(sec.fundamentals_source)}.</div>` : ""}
        <p><b>נתונים נוספים:</b> ${ch ? `${ch.kind === "etf" ? "קרן הסקטור" : "סל הפרוקסי"} (USD) — תשואה בשנה ${pct(ch.returns["1Y"])}, ב-5 שנים ${pct(ch.returns["5Y"])}, תנודתיות שנתית ${pct(ch.returns.vol1Y, 1, false)}, ירידה מקסימלית בשנה ${pct(ch.returns.maxDD1Y, 1)}.` : NA}</p>
        <div id="chSec${i}" class="chart sm"></div>
        <div class="src">${ch ? `<b>${esc(ch.label)}.</b> ${esc(ch.method)}${ch.kind === "proxy" ? ` ${ch.n_used} ניירות; כיסוי משקל ביום האחרון ${fmt(ch.coverage_last, 1)}%.` : ""} מטבע: ${esc(ch.currency)}.${ch.kind === "proxy" ? ` <b>סדרה מוטמעת (לא מתרעננת חי), עד ${dateHe(ch.returns && ch.returns.last_date)}.</b>` : ch._live ? " <b>עודכן חי.</b>" : ""}` : "גרף: לא זמין — חסרים נתוני מחיר לרכיבים."}</div>
      </div>`;
    });
    h += `</section>`;

    h += `<section class="card" id="s-all"><h2>כל הסקטורים יחד מול השוק (מנורמל ל-100, USD)</h2>${eqItems.some(s => s.chart) && D.market_usd && D.market_usd.series ? `<div id="chAll" class="chart" style="height:480px"></div>
      <div class="src">סקטורים = ${D.sectors.items.some(s => s.chart && s.chart.kind === "etf") ? "קרנות סקטור" : "סלי פרוקסי מחושבים (ראו שיטה בכל סקטור)"}; קו השוק = ${esc(D.market_usd.label)}. כולם בדולר. לחיצה על פריט במקרא מסתירה/מציגה אותו.</div>` : `<p>${NA}</p>`}</section>`;

    const srcs = [];
    srcs.push("<li>מחירים, מדדים ושערי חליפין: Yahoo Finance דרך yfinance (מחירי סגירה מותאמים).</li>");
    if (sec) {
      srcs.push(`<li>משקלי סקטורים: ${link(sec.url, sec.url)} (נכון ל-${esc(sec.as_of)}).</li>`);
      if (sec.holdings_url !== sec.url) srcs.push(`<li>רכיבי הקרן: ${link(sec.holdings_url, sec.holdings_url)} (נכון ל-${esc(sec.holdings_as_of)}).</li>`);
    }
    (D.fund_exposure || []).forEach(f => srcs.push(`<li>${esc(f.name)}: ${link(f.page, "עמוד הקרן")} (נכון ל-${esc(f.as_of)}).</li>`));
    srcs.push("<li>אין בעמוד תחזיות או המלצות; כל המספרים היסטוריים.</li>");
    h += `<section class="card" id="s-src"><h2>מקורות, שיטה ופערים</h2><ul class="gaps">${srcs.join("")}</ul>
      <h3>פערים / נתונים חסרים</h3>${(D.gaps || []).length ? `<ul class="gaps">${D.gaps.map(g => `<li>${esc(g)}</li>`).join("")}</ul>` : "<p>אין.</p>"}</section>`;

    $("#app").innerHTML = h;
    if ($("#mainSel") && keepSel && D._rerender) { $("#mainSel").value = keepSel; $("#mainNorm").checked = keepNorm; }
    D._rerender = false;
    if ($("#mainSel")) { $("#mainSel").onchange = drawMain; $("#mainNorm").onchange = drawMain; }
    drawAll();
    if (!D._live && !D._liveRunning) liveRefresh(D);
  }

  function seriesList(D) {
    const L = [];
    if (D.index) L.push({ k: "idx", label: `${D.index.name_he} (${D.index.ticker}, ${D.index.currency})`, s: D.index.series, r: D.index.returns, obj: D.index });
    (D.extra_series || []).forEach((e, i) => L.push({ k: "x" + i, label: e.label, s: e.series, r: e.returns, obj: e }));
    (D.fx || []).forEach((e, i) => L.push({ k: "f" + i, label: `${e.ticker} — ${e.label}`, s: e.series, r: e.returns, obj: e }));
    return L;
  }

  function drawMain() {
    if (!$("#chMain")) return;
    const D = current, st = marketStart(D), L = seriesList(D);
    if (!L.length) return;
    if ($("#mainNorm").checked) {
      Plotly.react("chMain", L.map((o, i) => line(rebase(cut(S(o.s), st)), o.label, COLORS[i % COLORS.length])), LAYOUT(`השוואה מנורמלת — ${RANGE_HE[range]}`, "100 = תחילת הטווח"), CFG());
      return;
    }
    const o = L.find(x => x.k === $("#mainSel").value) || L[0];
    const s = cut(S(o.s), st);
    const lay = LAYOUT(`${o.label} — ${RANGE_HE[range]}`, "רמה");
    if (s.v.length) { let mn = Infinity, mx = -Infinity; s.v.forEach(x => { if (x < mn) mn = x; if (x > mx) mx = x; }); const pad = (mx - mn) * 0.05 || 1; lay.yaxis.range = [mn - pad, mx + pad]; }
    Plotly.react("chMain", [line(s, o.label, COLORS[0], { fill: "tozeroy", fillcolor: "rgba(31,111,235,0.06)" })], lay, CFG());
  }

  function drawPie() {
    const sec = current.sectors; if (!sec || !$("#chPie")) return;
    const it = sec.items.filter(s => s.weight > 0), m = isMobile();
    Plotly.react("chPie", [{
      type: "pie", labels: it.map(s => bidi(s.sector_he)), values: it.map(s => s.weight), sort: false, direction: "clockwise",
      textinfo: m ? "percent" : "label+percent", textposition: "inside", insidetextorientation: "horizontal", hole: 0.35,
      marker: { colors: it.map(s => COLORS[sec.items.indexOf(s) % COLORS.length]), line: { color: "#fff", width: 1 } },
      hovertemplate: "%{label}<br>%{value:.2f}% מהקרן<extra></extra>",
    }], Object.assign(LAYOUT(`${sec.fund} — משקלי סקטורים, ${sec.as_of}`), m ? { showlegend: true, legend: { orientation: "h", x: 0.5, xanchor: "center", y: -0.02, yanchor: "top", font: { size: 11 } }, margin: { l: 4, r: 4, t: 40, b: 4 } }
      : { showlegend: false, margin: { l: 10, r: 10, t: 48, b: 10 } }), CFG());
  }

  function marketLine(D) { return D.market_usd && S(D.market_usd.series); }

  function drawSectors() {
    const D = current, st = marketStart(D); if (!D.sectors) return;
    const mk = marketLine(D);
    D.sectors.items.forEach((s, i) => {
      const el = document.getElementById("chSec" + i); if (!el) return;
      if (!s.chart) { el.innerHTML = `<p class="na">גרף לא זמין</p>`; el.style.height = "auto"; return; }
      lazy(el, () => {
      const cs = S(s.chart.series);
      let first = cut(cs, st).d[0] || st.toISOString().slice(0, 10);
      if (mk) { const m0 = cut(mk, st).d[0]; if (m0 && m0 > first) first = m0; }
      const tr = [line(rebase(cut(cs, first)), s.chart.kind === "etf" ? s.chart.ticker : `פרוקסי ${s.sector_he}`, COLORS[i % COLORS.length], { line: { width: 2.2, color: COLORS[i % COLORS.length] } })];
      if (mk) tr.push(line(rebase(cut(mk, first)), D.market_usd.label + " (USD)", "#868e96", { line: { width: 1.4, dash: "dot", color: "#868e96" } }));
      Plotly.react(el, tr, LAYOUT(`${s.sector_he} — ${s.chart.kind === "etf" ? "קרן סקטור" : "פרוקסי מחושב"} מול השוק (USD), ${RANGE_HE[range]}`, "100 = תחילת הטווח"), CFG());
      });
    });
  }

  function drawCombined() {
    const D = current, st = marketStart(D); if (!D.sectors || !$("#chAll")) return;
    lazy($("#chAll"), () => {
    const mk = marketLine(D);
    const items = D.sectors.items.filter(s => s.chart);
    let first = cut(mk, st).d[0];
    items.forEach(s => { const c = cut(S(s.chart.series), st); if (c.d.length && c.d[0] > first) first = c.d[0]; });
    const tr = items.map(s => line(rebase(cut(S(s.chart.series), first)), s.sector_he + (s.chart.kind === "etf" ? ` (${s.chart.ticker})` : ""), COLORS[D.sectors.items.indexOf(s) % COLORS.length]));
    tr.push(line(rebase(cut(mk, first)), D.market_usd.label, "#111", { line: { width: 3, color: "#111" } }));
    Plotly.react("chAll", tr, LAYOUT(`כל הסקטורים מול השוק — ${RANGE_HE[range]} (מ-${dateHe(first)})`, "100 = תחילת הטווח"), CFG());
    });
  }

  function drawAll() {
    if (!current) return;
    const l = $("#rangeLbl"); if (l) l.textContent = RANGE_HE[range];
    drawMain(); drawPie(); drawSectors(); drawCombined();
    document.body.setAttribute("data-rendered", current.id);
  }


  // ================================================================ live refresh (browser-side, on every page open)
  // Source: CNBC public quote/chart endpoints (keyless; send Access-Control-Allow-Origin: *), so they work from file://.
  // Daily bars (~1 year) are spliced onto the embedded Yahoo history at the last common day (embedded part rescaled by the
  // median price ratio of the last 10 common days, so returns are unchanged and the newest value is the live price); the latest quote is appended
  // when its session date is newer than the last daily bar. Nothing is invented: a series that cannot be fetched or
  // fails the consistency check keeps its embedded data and is listed in the banner.
  const CNBC_CHART = "https://ts-api.cnbc.com/harmony/app/charts/1M.json?symbol=";   // "1M" on this endpoint = ~1 year of daily bars
  const CNBC_QUOTE = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&symbols=";
  const LIVE_SYM = {
    "^BVSP": ".BVSP", "^MXX": ".MXX", "^TWII": ".TWII", "^KS11": ".KS11", "^NSEI": ".NSEI", "000001.SS": ".SSEC", "^HSI": ".HSI",
    "^STI": ".STI", "^N225": ".N225", "^AXJO": ".AXJO", "^FTSE": ".FTSE", "^GSPC": ".SPX", "TA35.TA": ".TA35", "^TA125.TA": ".TA125",
    "GD.AT": ".ATG", "XSFD.L": "XSFD",
    "^MERV": null, "ICOLCAP.CL": null,   // not available on the CNBC endpoints -> stay embedded
  };
  const INVERT_FX = { EUR: 1, GBP: 1, AUD: 1 };   // CNBC quotes these as USD per unit; Yahoo XXX=X is units per USD
  function liveSym(y) {
    if (!y) return null;
    if (y in LIVE_SYM) return LIVE_SYM[y] ? { s: LIVE_SYM[y], inv: false } : null;
    const m = /^([A-Z]{3})=X$/.exec(y);
    if (m) return { s: m[1] + "=", inv: !!INVERT_FX[m[1]], fx: true };
    if (/^[A-Z]{2,5}$/.test(y)) return { s: y, inv: false };
    return null;
  }
  const LIVE_CACHE = {};   // per page load; symbol -> promise
  function fetchJSON(url, ms) {
    const ac = new AbortController(); const t = setTimeout(() => ac.abort(), ms || 15000);
    return fetch(url, { signal: ac.signal, cache: "no-store" }).then(r => { clearTimeout(t); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }
  function getBars(sym) {
    if (!LIVE_CACHE["b:" + sym]) LIVE_CACHE["b:" + sym] = fetchJSON(CNBC_CHART + encodeURIComponent(sym)).catch(() => new Promise(r => setTimeout(r, 500)).then(() => fetchJSON(CNBC_CHART + encodeURIComponent(sym)))).then(j => {
      const b = (j && j.barData && j.barData.priceBars) || [];
      const out = { d: [], v: [] };
      b.forEach(x => { const c = parseFloat(x.close); const t = String(x.tradeTime || ""); if (isFinite(c) && c > 0 && t.length >= 8) { out.d.push(`${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}`); out.v.push(c); } });
      if (out.d.length < 5) throw new Error("אין נתוני מחיר יומיים");
      return out;
    });
    return LIVE_CACHE["b:" + sym];
  }
  function getQuotes(syms) {
    const key = "q:" + syms.join("|");
    const url = CNBC_QUOTE + encodeURIComponent(syms.join("|"));
    if (!LIVE_CACHE[key]) LIVE_CACHE[key] = fetchJSON(url).catch(() => new Promise(r => setTimeout(r, 400)).then(() => fetchJSON(url))).catch(() => new Promise(r => setTimeout(r, 1200)).then(() => fetchJSON(url))).then(j => {
      const out = {}; delete LIVE_CACHE.quoteErr;
      ((j && j.FormattedQuoteResult && j.FormattedQuoteResult.FormattedQuote) || []).forEach(q => {
        const v = parseFloat(String(q.last || "").replace(/,/g, ""));
        if (q.code === 0 || q.code === "0") if (isFinite(v) && v > 0 && q.last_time) out[q.symbol] = { v, time: q.last_time, date: String(q.last_time).slice(0, 10) };
      });
      return out;
    }).catch(e => { LIVE_CACHE.quoteErr = String(e && e.message || e); return {}; });
    return LIVE_CACHE[key];
  }
  function splice(emb, live, tol) {
    // emb, live: {d, v}. Anchor on the latest common day (the embedded last point may be intraday, so prefer an earlier day
    // unless both agree). Scale live by emb/live at the anchor so returns after the anchor come from the live source.
    const ei = new Map(emb.d.map((d, i) => [d, i])), embLast = emb.d[emb.d.length - 1];
    let a = -1;
    for (let j = live.d.length - 1; j >= 0; j--) {
      const d = live.d[j]; if (!ei.has(d)) continue;
      if (d < embLast || Math.abs(emb.v[ei.get(d)] / live.v[j] - 1) < 1e-4) { a = j; break; }
    }
    if (a < 0) throw new Error("אין חפיפה בין הנתונים המוטמעים לנתונים החיים (הנתונים המוטמעים ישנים משנה — הריצו update_data.py)");
    // scale factor = median emb/live ratio over the last (up to) 10 common days — robust to a single bad print in either source
    const rs = [];
    for (let j = a; j >= 0 && rs.length < 10; j--) if (ei.has(live.d[j])) rs.push(emb.v[ei.get(live.d[j])] / live.v[j]);
    rs.sort((x, y) => x - y);
    const k = rs.length % 2 ? rs[(rs.length - 1) / 2] : (rs[rs.length / 2 - 1] + rs[rs.length / 2]) / 2;
    if (!(Math.abs(k - 1) <= tol)) throw new Error(`אי-התאמה בין המקורות (יחס ${k.toFixed(3)}) — נשמרו הנתונים המוטמעים`);
    const cutI = ei.get(live.d[a]);
    // the embedded history is rescaled by 1/k (like a back-adjustment) so the latest value shown is the live source's own price
    const out = { d: emb.d.slice(0, cutI + 1), v: k === 1 ? emb.v.slice(0, cutI + 1) : emb.v.slice(0, cutI + 1).map(x => +(x / k).toPrecision(8)) };
    for (let j = a + 1; j < live.d.length; j++) { out.d.push(live.d[j]); out.v.push(live.v[j]); }
    return { s: out, k, anchor: live.d[a] };
  }
  // JS port of update_data.ret_table
  function addMonths(iso, m) {
    const [y, mo, d] = iso.split("-").map(Number); const t = new Date(Date.UTC(y, mo - 1 + m, 1));
    const dim = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + 1, 0)).getUTCDate();
    t.setUTCDate(Math.min(d, dim)); return t.toISOString().slice(0, 10);
  }
  function retTable(s) {
    const n = s.v.length; if (n < 2) return {};
    const last = s.v[n - 1], ld = s.d[n - 1];
    const at = date => { if (s.d[0] > date) return null; let i = n - 1; while (i > 0 && s.d[i] > date) i--; return s.v[i]; };
    const chg = date => { const v = at(date); return v ? +((last / v - 1) * 100).toFixed(2) : null; };
    const r = { last: +last.toFixed(4), last_date: ld, "1D": +((last / s.v[n - 2] - 1) * 100).toFixed(2),
      "1M": chg(addMonths(ld, -1)), "3M": chg(addMonths(ld, -3)), YTD: chg(`${+ld.slice(0, 4) - 1}-12-31`),
      "1Y": chg(addMonths(ld, -12)), "3Y": chg(addMonths(ld, -36)), "5Y": chg(addMonths(ld, -60)), first_date: s.d[0] };
    const y1 = addMonths(ld, -12); const w = []; s.d.forEach((d, i) => { if (d >= y1) w.push(s.v[i]); });
    const rets = []; for (let i = 1; i < w.length; i++) rets.push(w[i] / w[i - 1] - 1);
    if (rets.length > 50) { const m = rets.reduce((a, b) => a + b, 0) / rets.length; const sd = Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / (rets.length - 1)); r.vol1Y = +(sd * Math.sqrt(252) * 100).toFixed(2); } else r.vol1Y = null;
    if (w.length > 50) { let pk = -Infinity, dd = 0; w.forEach(v => { pk = Math.max(pk, v); dd = Math.min(dd, v / pk - 1); }); r.maxDD1Y = +(dd * 100).toFixed(2); } else r.maxDD1Y = null;
    return r;
  }
  function divideFF(a, f) {   // a / f with f forward-filled onto a's dates (same as pandas reindex().ffill())
    const out = { d: [], v: [] }; let j = 0, cur = null;
    for (let i = 0; i < a.d.length; i++) { while (j < f.d.length && f.d[j] <= a.d[i]) { cur = f.v[j]; j++; } if (cur) { out.d.push(a.d[i]); out.v.push(+(a.v[i] / cur).toPrecision(8)); } }
    return out;
  }
  function srcTag(o) {
    if (!o) return "";
    if (o._live) return `<span class="tag live-ok" title="${escA(o._live.note)}">חי · ${esc(o._live.asof)}</span>`;
    if (o._liveErr) return `<span class="tag live-no" title="${escA(o._liveErr)}">מוטמע</span>`;
    return `<span class="tag">מוטמע</span>`;
  }
  async function refreshOne(obj, ticker, quotes) {
    const ls = liveSym(ticker);
    if (!ls) { obj._liveErr = "אין מקבילה במקור החי (CNBC) לסדרה זו"; return false; }
    try {
      let live = await getBars(ls.s);
      if (ls.inv) live = { d: live.d, v: live.v.map(x => 1 / x) };
      const q = quotes[ls.s];
      let asof = live.d[live.d.length - 1], intraday = false;
      if (q && q.date > asof) { live = { d: live.d.concat([q.date]), v: live.v.concat([ls.inv ? 1 / q.v : q.v]) }; asof = q.date; intraday = true; }
      const sp = splice(S(obj.series), live, ls.fx ? 0.06 : 0.03);
      obj.series = sp.s; obj.returns = retTable(sp.s);
      obj._live = { asof: dateHe(asof), note: `CNBC ${ls.s}: סגירות יומיות עד ${live.d[live.d.length - (intraday ? 2 : 1)]}${intraday ? `, ומחיר אחרון ${q.time} (עשוי להיות מושהה)` : ""}; עוגן ${sp.anchor}, יחס התאמה ${sp.k.toFixed(4)}` };
      delete obj._liveErr; return true;
    } catch (e) { obj._liveErr = String(e && e.message || e); return false; }
  }
  async function liveRefresh(D) {
    D._liveRunning = true;
    const jobs = [];   // [obj, yahooTicker]
    if (D.index) jobs.push([D.index, D.index.ticker]);
    (D.extra_series || []).forEach(e => { if (!/ USD$/.test(e.ticker)) jobs.push([e, e.ticker]); });
    (D.fx || []).forEach(e => jobs.push([e, e.ticker]));
    ((D.sectors && D.sectors.items) || []).forEach(it => { if (it.chart && it.chart.kind === "etf") jobs.push([it.chart, it.chart.ticker]); });
    const syms = [...new Set(jobs.map(j => liveSym(j[1])).filter(Boolean).map(x => x.s))];
    const quotes = syms.length ? await getQuotes(syms) : {};
    const res = await Promise.all(jobs.map(([o, t]) => refreshOne(o, t, quotes)));
    const ok = res.filter(Boolean).length;
    // derived USD series (index ÷ FX) and the market line used by the sector comparison
    const fx0 = (D.fx || [])[0];
    if (D.index && D.index._live) {
      const usd = fx0 ? divideFF(S(D.index.series), S(fx0.series)) : null;
      (D.extra_series || []).forEach(e => { if (/ USD$/.test(e.ticker) && usd) { e.series = usd; e.returns = retTable(usd); e._live = { asof: D.index._live.asof, note: "מחושב מחדש: המדד המעודכן ÷ שער החליפין" }; } });
      if (D.market_usd && D.market_usd.ticker === D.index.ticker) D.market_usd.series = (D.currency !== "USD" && D.index.kind !== "etf" && usd) ? usd : (D.index.currency === "USD" ? D.index.series : D.market_usd.series);
    }
    const now = new Date().toLocaleTimeString("he-IL", { timeZone: "Asia/Hebron", hour: "2-digit", minute: "2-digit" });
    const embDate = dateHe((D.index && D.index.returns && D.index.returns.last_date) || (fx0 && fx0.returns && fx0.returns.last_date));
    const failed = jobs.filter(j => !j[0]._live).map(j => `${j[1]} (${String(j[0]._liveErr || "לא עודכן").replace(/^(TypeError: )?Failed to fetch$/, "אין חיבור למקור / הבקשה נחסמה").replace(/^AbortError.*|.*aborted.*/, "תם הזמן")})`);
    const static_ = [];
    if (D.sectors) static_.push(`פילוח סקטורים ורכיבים — נכון ל-${D.sectors.as_of}`);
    if (D.sectors && D.sectors.items.some(i => i.chart && i.chart.kind === "proxy")) static_.push("סלי הסקטור המחושבים (פרוקסי) ונתוני המניות — מעודכנים לריצת update_data.py האחרונה");
    const srcTxt = `מקור חי: CNBC (ts-api.cnbc.com — סגירות יומיות; quote.cnbc.com — מחיר אחרון, עשוי להיות מושהה) · ההיסטוריה המוטמעת: Yahoo Finance, נוצרה ${dtHe(D.generated_at)}`;
    if (ok === 0) {
      D._live = { cls: "fail", html: `<b>נתונים נכון ל-${embDate}, רענון חי נכשל</b> — ${esc(failed.slice(0, 3).join(" · ") || "אין חיבור למקור")}. מוצגים הנתונים המוטמעים${D._offline ? " (עותק שמור במכשיר — אין חיבור לאינטרנט)" : ""}.` };
    } else {
      D._live = { cls: failed.length ? "partial" : "ok", html: `<b>עודכן חי ב-${now} (שעון ישראל)</b> — ${ok}/${jobs.length} סדרות מחיר עודכנו${LIVE_CACHE.quoteErr ? " (המחיר התוך-יומי לא נטען — מוצגת הסגירה היומית האחרונה)" : ""}. ${esc(srcTxt)}.` +
        (failed.length ? `<br>לא עודכנו (מוצגים נתונים מוטמעים): ${esc(failed.join(" · "))}` : "") +
        (static_.length ? `<br>נשארים סטטיים: ${esc(static_.join(" · "))}.` : "") };
    }
    D._liveRunning = false;
    document.body.setAttribute("data-live", D._live.cls);
    if (current === D) { const y = window.scrollY; D._rerender = true; render(D); window.scrollTo(0, y); }
  }

  // ---------------------------------------------------------------- loading
  function showError(msg) { $("#app").innerHTML = `<section class="card"><p>${msg}</p></section>`; document.body.setAttribute("data-rendered", "error"); }
  const FILE_HINT = "הדף נפתח כקובץ מקומי (file://) — הדפדפן חוסם טעינת קבצי נתונים כך. פתחו את כתובת האתר (https) או הריצו <code>python3 -m http.server</code> בתיקייה.";
  function load(id) {
    const MD = window.MARKET_DATA = window.MARKET_DATA || {};
    document.body.removeAttribute("data-rendered");
    if (MD[id]) { render(MD[id]); window.scrollTo(0, 0); return; }
    $("#app").innerHTML = '<p class="muted">טוען נתונים…</p>';
    fetch(`data/${encodeURIComponent(id)}.json`, { cache: "no-cache" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); const off = r.headers.get("x-sw-offline") === "1"; return r.json().then(D => { if (off) D._offline = true; return D; }); })
      .then(D => { MD[id] = D; if ($("#marketSelect").value === id) { render(D); window.scrollTo(0, 0); } })
      .catch(e => showError(`טעינת נתוני השוק נכשלה (${esc(e.message)}). ${location.protocol === "file:" ? FILE_HINT : "ייתכן שאין חיבור לאינטרנט ושוק זה טרם נשמר במכשיר — נסו שוב כשיש חיבור."}`));
  }

  async function init() {
    if (!window.MARKETS) {
      try { window.MARKETS = await fetch("data/markets.json", { cache: "no-cache" }).then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }); }
      catch (e) { return showError(`רשימת השווקים לא נטענה (${esc(e.message)}). ${location.protocol === "file:" ? FILE_HINT : "בדקו את החיבור ונסו שוב."}`); }
    }
    const list = (window.MARKETS || []).filter(m => !window.STANDALONE || window.STANDALONE.includes(m.id));
    const sel = $("#marketSelect");
    const groups = [];
    list.forEach(m => { let g = groups.find(x => x.r === m.region_he); if (!g) groups.push(g = { r: m.region_he, ms: [] }); g.ms.push(m); });
    sel.innerHTML = groups.map(g => `<optgroup label="${escA(g.r)}">${g.ms.map(m => `<option value="${escA(m.id)}">${escA(m.flag)} ${escA(m.name_he)}</option>`).join("")}</optgroup>`).join("");
    const hp = new URLSearchParams(location.hash.slice(1));
    const fromHash = hp.get("m");
    let saved = null; try { saved = localStorage.getItem("market"); } catch (e) { /* private mode */ }
    if (RANGE_HE[hp.get("r")]) { range = hp.get("r"); document.querySelectorAll("#rangeBar button").forEach(x => x.classList.toggle("on", x.dataset.r === range)); }
    const start = [fromHash, saved].find(x => x && list.some(m => m.id === x)) || (list[0] && list[0].id);
    if (!start) return showError("אין עדיין נתונים. הריצו update_data.py.");
    sel.value = start;
    const remember = () => { try { localStorage.setItem("market", sel.value); } catch (e) { /* ignore */ } };
    sel.onchange = () => { history.replaceState(null, "", "#m=" + sel.value + "&r=" + range); remember(); load(sel.value); };
    document.querySelectorAll("#rangeBar button").forEach(b => b.onclick = () => {
      document.querySelectorAll("#rangeBar button").forEach(x => x.classList.toggle("on", x === b));
      range = b.dataset.r; history.replaceState(null, "", "#m=" + sel.value + "&r=" + range); drawAll();
    });
    load(start);
    // redraw on rotation / crossing the mobile breakpoint (layout differs between phone and desktop)
    let lastMobile = isMobile();
    window.addEventListener("resize", () => { if (isMobile() !== lastMobile) { lastMobile = isMobile(); drawAll(); } });
  }
  document.addEventListener("DOMContentLoaded", init);
  // offline support (app shell + last viewed markets); only over http(s) and not in the single-file build
  if ("serviceWorker" in navigator && /^https?:$/.test(location.protocol) && !window.STANDALONE) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => { /* optional */ }));
  }
})();
