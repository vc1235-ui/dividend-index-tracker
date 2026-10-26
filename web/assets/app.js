(() => {
  "use strict";

  const LABEL = { BUY: "买入", SELL: "卖出", HOLD: "持有/观望" };
  const INDEX_SHORT = { "000015": "上证", "000922": "中证" };
  const $ = (s) => document.querySelector(s);
  const state = { summary: null, rows: [], sortKey: "score", asc: false, current: null, detail: null };
  const charts = {};

  // ---------- 工具 ----------
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const pct = (v, d = 2) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
  const num = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function quantile(arr, q) {
    const a = arr.filter((x) => x != null && isFinite(x)).sort((x, y) => x - y);
    if (!a.length) return null;
    const i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
    return a[lo] + (a[hi] - a[lo]) * (i - lo);
  }

  function movingAvg(arr, n) {
    const out = new Array(arr.length).fill(null);
    let sum = 0, cnt = 0;
    for (let i = 0; i < arr.length; i++) {
      if (arr[i] != null) { sum += arr[i]; cnt++; }
      if (i >= n && arr[i - n] != null) { sum -= arr[i - n]; cnt--; }
      if (i >= n - 1 && cnt) out[i] = +(sum / cnt).toFixed(3);
    }
    return out;
  }

  function chart(id) {
    if (!charts[id]) charts[id] = echarts.init(document.getElementById(id), null, { renderer: "canvas" });
    return charts[id];
  }

  function baseOption() {
    const text = css("--text"), muted = css("--muted"), border = css("--border");
    return {
      backgroundColor: "transparent",
      textStyle: { color: text, fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { left: 48, right: 52, top: 30, bottom: 30, containLabel: false },
      tooltip: { trigger: "axis", backgroundColor: css("--surface"), borderColor: border, textStyle: { color: text } },
      legend: { top: 0, textStyle: { color: muted }, itemWidth: 14, itemHeight: 8 },
      xAxis: { type: "category", axisLine: { lineStyle: { color: border } }, axisLabel: { color: muted } },
      yAxis: { type: "value", scale: true, axisLabel: { color: muted }, splitLine: { lineStyle: { color: border } } },
    };
  }

  // ---------- 主题 ----------
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("theme"); } catch (e) { /* 忽略 */ }
    if (saved) document.documentElement.dataset.theme = saved;
    $("#themeBtn").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) { /* 忽略 */ }
      renderAllCharts();
    });
  }

  // ---------- 数据加载 ----------
  async function load() {
    try {
      const [summary, log] = await Promise.all([
        fetch("data/summary.json", { cache: "no-cache" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch("data/signals_log.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : [])).catch(() => []),
      ]);
      state.summary = summary;
      state.log = log;
    } catch (e) {
      const b = $("#errorBanner");
      b.hidden = false;
      b.textContent = "数据加载失败：尚未生成数据，或数据更新任务未成功运行。请在仓库 Actions 中手动运行“更新数据并部署”。";
      return;
    }
    const s = state.summary;
    $("#dataDate").textContent = s.data_date;
    $("#genAt").textContent = s.generated_at;
    $("#demoBanner").hidden = !s.demo;
    renderKpis();
    renderRules();
    renderLog();
    bindFilters();
    renderTable();
    renderScatter();
    const hash = location.hash.slice(1);
    if (hash && s.stocks.some((x) => x.code === hash)) openDetail(hash, false);
  }

  // ---------- KPI ----------
  function renderKpis() {
    const st = state.summary.stocks;
    const cnt = (sig) => st.filter((x) => x.signal === sig).length;
    const yields = st.map((x) => x.yield).filter((x) => x != null);
    const items = [
      ["跟踪个股", st.length, ""],
      ["买入信号", cnt("BUY"), "buy"],
      ["卖出信号", cnt("SELL"), "sell"],
      ["通过质量筛选", st.filter((x) => x.quality).length, ""],
      ["股息率中位数", pct(quantile(yields, 0.5)), ""],
    ];
    $("#kpis").innerHTML = items.map(([l, v, c]) => `<div class="kpi ${c}"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
  }

  // ---------- 表格 ----------
  function bindFilters() {
    ["#fIndex", "#fSignal", "#fQuality", "#fSearch"].forEach((id) => $(id).addEventListener("input", () => { renderTable(); renderScatter(); }));
    document.querySelectorAll("#table th").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.sortKey === k) state.asc = !state.asc; else { state.sortKey = k; state.asc = k === "name" || k === "pe" || k === "pe_pct"; }
      renderTable();
    }));
  }

  function filtered() {
    const idx = $("#fIndex").value, sig = $("#fSignal").value, q = $("#fSearch").value.trim(), quality = $("#fQuality").checked;
    return state.summary.stocks.filter((s) => {
      const codes = Object.keys(s.indices);
      if (idx === "both" && codes.length < 2) return false;
      if (idx && idx !== "both" && !codes.includes(idx)) return false;
      if (sig && s.signal !== sig) return false;
      if (quality && !s.quality) return false;
      if (q && !s.code.includes(q) && !s.name.includes(q)) return false;
      return true;
    });
  }

  function renderTable() {
    const rows = filtered();
    const k = state.sortKey, dir = state.asc ? 1 : -1;
    const order = { BUY: 0, HOLD: 1, SELL: 2 };
    rows.sort((a, b) => {
      let x = a[k], y = b[k];
      if (k === "signal") { x = order[x]; y = order[y]; }
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y, "zh") : x - y) * dir;
    });
    state.rows = rows;
    document.querySelectorAll("#table th").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.k === k);
      th.classList.toggle("asc", th.dataset.k === k && state.asc);
    });
    const chg = (v) => (v == null ? "—" : `<span class="${v >= 0 ? "up" : "down"}">${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%</span>`);
    $("#table tbody").innerHTML = rows.map((s) => `
      <tr data-code="${s.code}" class="${state.current === s.code ? "active" : ""}">
        <td>${esc(s.name)}<span class="code">${s.code}</span>${Object.keys(s.indices).map((c) => `<span class="tag">${INDEX_SHORT[c] || c}</span>`).join("")}</td>
        <td><span class="sig ${s.signal}">${LABEL[s.signal]}</span></td>
        <td class="num">${num(s.score, 0)}</td>
        <td class="num">${num(s.price)}</td>
        <td class="num"><b>${pct(s.yield)}</b></td>
        <td class="num">${s.yield_pct == null ? "—" : s.yield_pct.toFixed(0) + "%"}</td>
        <td class="num ${s.pe != null && s.pe <= 0 ? "up" : ""}">${num(s.pe, 1)}</td>
        <td class="num">${s.pe_pct == null ? "—" : s.pe_pct.toFixed(0) + "%"}</td>
        <td class="num">${num(s.pb)}</td>
        <td class="num">${s.years} 年</td>
        <td class="num">${pct(s.payout, 0)}</td>
        <td class="num">${pct(s.dps_cagr, 1)}</td>
        <td class="num">${chg(s.change_1y)}</td>
      </tr>`).join("");
    $("#countNote").textContent = `显示 ${rows.length} / ${state.summary.stocks.length} 只。点击表头排序，点击行查看图表。`
      + (state.summary.failed && state.summary.failed.length ? ` 本次有 ${state.summary.failed.length} 只数据获取失败：${state.summary.failed.join("、")}。` : "");
    document.querySelectorAll("#table tbody tr").forEach((tr) => tr.addEventListener("click", () => openDetail(tr.dataset.code, true)));
  }

  // ---------- 散点图 ----------
  function renderScatter() {
    if (!state.summary) return;
    const rules = state.summary.rules;
    const rows = filtered().filter((s) => s.yield != null && s.yield_pct != null);
    const maxCap = Math.max(...rows.map((s) => s.mcap || 0), 1);
    const color = { BUY: css("--buy"), SELL: css("--sell"), HOLD: css("--hold") };
    const series = ["BUY", "HOLD", "SELL"].map((sig) => ({
      name: LABEL[sig], type: "scatter",
      itemStyle: { color: color[sig], opacity: 0.8 },
      symbolSize: (v) => 8 + 26 * Math.sqrt((v[2] || 0) / maxCap),
      data: rows.filter((s) => s.signal === sig).map((s) => ({ value: [+(s.yield * 100).toFixed(2), s.yield_pct, s.mcap || 0], code: s.code, name: s.name, s })),
    }));
    series[0].markLine = {
      silent: true, symbol: "none", lineStyle: { type: "dashed", color: css("--muted") }, label: { color: css("--muted"), formatter: "{b}", position: "insideEndTop" },
      data: [
        { name: `买入股息率 ${rules.buy.min_yield * 100}%`, xAxis: rules.buy.min_yield * 100 },
        { name: `卖出股息率 ${rules.sell.max_yield * 100}%`, xAxis: rules.sell.max_yield * 100 },
        { name: `买入分位 ${rules.buy.min_yield_percentile}%`, yAxis: rules.buy.min_yield_percentile },
      ],
    };
    const opt = baseOption();
    Object.assign(opt, {
      grid: { left: 56, right: 24, top: 34, bottom: 44 },
      legend: { top: 0, right: 0, textStyle: { color: css("--muted") }, itemWidth: 10, itemHeight: 10 },
      tooltip: {
        trigger: "item", backgroundColor: css("--surface"), borderColor: css("--border"), textStyle: { color: css("--text") },
        formatter: (p) => {
          const s = p.data.s;
          return `<b>${esc(s.name)}</b> ${s.code}<br>${LABEL[s.signal]} · 评分 ${num(s.score, 0)}<br>股息率 ${pct(s.yield)}（分位 ${num(s.yield_pct, 0)}%）<br>PE ${num(s.pe, 1)}（分位 ${s.pe_pct == null ? "—" : s.pe_pct.toFixed(0) + "%"}）`;
        },
      },
      xAxis: { type: "value", name: "股息率TTM（%）", nameLocation: "middle", nameGap: 26, scale: true, axisLabel: { color: css("--muted") }, nameTextStyle: { color: css("--muted") }, splitLine: { lineStyle: { color: css("--border") } } },
      yAxis: { type: "value", name: "股息率历史分位（%）", nameLocation: "middle", nameGap: 36, min: 0, max: 100, axisLabel: { color: css("--muted") }, nameTextStyle: { color: css("--muted") }, splitLine: { lineStyle: { color: css("--border") } } },
      series,
    });
    const c = chart("scatter");
    c.setOption(opt, true);
    c.off("click");
    c.on("click", (p) => p.data && openDetail(p.data.code, true));
  }

  // ---------- 个股详情 ----------
  async function openDetail(code, scroll) {
    const s = state.summary.stocks.find((x) => x.code === code);
    if (!s) return;
    state.current = code;
    history.replaceState(null, "", "#" + code);
    renderTable();
    $("#detail").hidden = false;
    $("#dTitle").innerHTML = `${esc(s.name)} <span class="code">${s.code}</span> <span class="sig ${s.signal}">${LABEL[s.signal]}</span>`;
    const since = s.since ? `<span>信号自 ${s.since}</span>` : "";
    const parts = s.score_parts ? `<span>评分 ${num(s.score, 0)} = 股息率 ${s.score_parts.yield} + 稳定性 ${s.score_parts.stability} + 支付率 ${s.score_parts.payout} + 估值 ${s.score_parts.valuation} + 增长 ${s.score_parts.growth}</span>` : "";
    $("#dReasons").innerHTML = s.reasons.map((r) => `<span>${esc(r)}</span>`).join("") + since + parts;
    if (scroll) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      state.detail = await fetch(`data/stocks/${code}.json`, { cache: "no-cache" }).then((r) => r.json());
    } catch (e) {
      state.detail = null;
      return;
    }
    renderDetailCharts();
  }

  function renderDetailCharts() {
    const d = state.detail;
    if (!d || $("#detail").hidden) return;
    const rules = state.summary.rules;
    const accent = css("--accent"), muted = css("--muted"), buy = css("--buy"), sell = css("--sell");
    const yieldPct = d.yield.map((v) => (v == null ? null : +(v * 100).toFixed(2)));
    const zoom = [{ type: "inside", start: 0, end: 100 }];

    // 1. 股价与股息率
    let o = baseOption();
    o.legend.data = ["收盘价", "年线 MA250", "股息率TTM"];
    o.xAxis.data = d.dates;
    o.yAxis = [o.yAxis, { type: "value", scale: true, axisLabel: { color: muted, formatter: "{value}%" }, splitLine: { show: false } }];
    o.dataZoom = zoom;
    o.series = [
      { name: "收盘价", type: "line", data: d.close, showSymbol: false, lineStyle: { width: 1.5, color: accent }, itemStyle: { color: accent },
        markPoint: { symbol: "pin", symbolSize: 26, itemStyle: { color: buy }, label: { fontSize: 9, formatter: "息" },
          data: d.events.map((e) => ({ coord: [e.ex_date, d.close[d.dates.indexOf(e.ex_date)] ?? null], value: e.dps })).filter((p) => p.coord[1] != null) } },
      { name: "年线 MA250", type: "line", data: movingAvg(d.close, 250), showSymbol: false, lineStyle: { width: 1, type: "dashed", color: muted }, itemStyle: { color: muted } },
      { name: "股息率TTM", type: "line", yAxisIndex: 1, data: yieldPct, showSymbol: false, lineStyle: { width: 1.2, color: buy }, itemStyle: { color: buy } },
    ];
    chart("cPrice").setOption(o, true);

    // 2. 股息率区间
    o = baseOption();
    o.legend.show = false;
    o.xAxis.data = d.dates;
    o.yAxis.axisLabel.formatter = "{value}%";
    o.dataZoom = zoom;
    const q = (p) => +quantile(yieldPct, p).toFixed(2);
    o.series = [{
      name: "股息率TTM", type: "line", data: yieldPct, showSymbol: false, lineStyle: { width: 1.5, color: accent }, itemStyle: { color: accent },
      areaStyle: { opacity: 0.08, color: accent },
      markLine: {
        symbol: "none", label: { formatter: "{b} {c}%", position: "insideEndTop", color: muted, fontSize: 11 },
        data: [
          { name: "买入线", yAxis: rules.buy.min_yield * 100, lineStyle: { color: buy, type: "dashed" } },
          { name: "卖出线", yAxis: rules.sell.max_yield * 100, lineStyle: { color: sell, type: "dashed" } },
          { name: `${rules.buy.min_yield_percentile}%分位`, yAxis: q(rules.buy.min_yield_percentile / 100), lineStyle: { color: buy, type: "dotted", opacity: 0.7 } },
          { name: `${rules.sell.max_yield_percentile}%分位`, yAxis: q(rules.sell.max_yield_percentile / 100), lineStyle: { color: sell, type: "dotted", opacity: 0.7 } },
        ],
      },
    }];
    chart("cYield").setOption(o, true);

    // 3. PE 分位带
    o = baseOption();
    o.legend.show = false;
    o.xAxis.data = d.dates;
    o.dataZoom = zoom;
    const posPe = d.pe.filter((v) => v != null && v > 0);
    const peLines = posPe.length > 20 ? [[0.2, "20%分位", sell], [0.5, "中位数", muted], [0.8, "80%分位", buy]].map(([p, n, c]) => ({ name: n, yAxis: +quantile(posPe, p).toFixed(2), lineStyle: { color: c, type: "dashed" } })) : [];
    o.series = [{
      name: "PE(TTM)", type: "line", data: d.pe, showSymbol: false, lineStyle: { width: 1.5, color: accent }, itemStyle: { color: accent },
      markLine: { symbol: "none", label: { formatter: "{b} {c}", position: "insideEndTop", color: muted, fontSize: 11 }, data: peLines },
    }];
    if (!posPe.length) o.graphic = { type: "text", left: "center", top: "middle", style: { text: "暂无 PE 数据", fill: muted } };
    chart("cPe").setOption(o, true);

    // 4. 年度分红
    o = baseOption();
    o.legend.data = ["每股分红(元)", "支付率"];
    o.xAxis.data = d.annual.map((r) => r.year);
    o.yAxis = [o.yAxis, { type: "value", min: 0, axisLabel: { color: muted, formatter: "{value}%" }, splitLine: { show: false } }];
    o.yAxis[0].scale = false;
    o.series = [
      { name: "每股分红(元)", type: "bar", data: d.annual.map((r) => r.dps), itemStyle: { color: accent, borderRadius: [3, 3, 0, 0] }, barMaxWidth: 28 },
      { name: "支付率", type: "line", yAxisIndex: 1, data: d.annual.map((r) => (r.payout == null ? null : +(r.payout * 100).toFixed(1))), itemStyle: { color: buy }, lineStyle: { color: buy }, connectNulls: true },
    ];
    chart("cDiv").setOption(o, true);
  }

  function renderAllCharts() {
    renderScatter();
    renderDetailCharts();
  }

  $("#dClose").addEventListener("click", () => {
    $("#detail").hidden = true;
    state.current = null;
    history.replaceState(null, "", location.pathname);
    renderTable();
  });

  // ---------- 记录与规则 ----------
  function renderLog() {
    const log = state.log || [];
    $("#log").innerHTML = log.length
      ? log.slice(0, 200).map((a) => `<li><span class="date">${a.date}</span><span class="sig ${a.signal}">${LABEL[a.signal]}</span><b>${esc(a.name)}</b><span class="code">${a.code}</span><span class="why">${(a.reasons || []).map(esc).join("；")}</span></li>`).join("")
      : `<li class="dim">暂无提醒记录。信号由“持有/观望”变为“买入”或“卖出”时会记录并推送。</li>`;
  }

  function renderRules() {
    const r = state.summary.rules, n = state.summary.history_years;
    $("#rules").innerHTML = `
      <p><b>质量筛选（值得投资）</b></p>
      <ul>
        <li>连续现金分红 ≥ ${r.quality.min_consecutive_years} 年，PE(TTM) &gt; 0（盈利）</li>
        <li>最近完整年度股利支付率 ≤ ${(r.quality.max_payout_ratio * 100).toFixed(0)}%</li>
        <li>综合评分 ≥ ${r.quality.min_score}（股息率 30 + 分红稳定性 20 + 支付率 15 + 估值分位 20 + 分红增长 15）</li>
      </ul>
      <p><b class="up">买入</b>：通过质量筛选，且股息率 ≥ ${(r.buy.min_yield * 100).toFixed(1)}%、股息率处于近 ${n} 年 ≥ ${r.buy.min_yield_percentile}% 分位、PE 分位 ≤ ${r.buy.max_pe_percentile}%</p>
      <p><b class="down">卖出</b>（任一）：股息率 ≤ ${(r.sell.max_yield * 100).toFixed(1)}%；股息率处于近 ${n} 年 ≤ ${r.sell.max_yield_percentile}% 分位；PE 为负；年度分红较上年削减超过 ${((1 - r.sell.dividend_cut_ratio) * 100).toFixed(0)}%；被调出指数</p>
      <p class="dim">阈值可在仓库的 config.yaml 中修改。</p>`;
  }

  addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
  initTheme();
  load();
})();
