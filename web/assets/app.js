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
  // 信号徽标：超出每日名额的买入候选单独标出
  const sigBadge = (s) => s.buy_candidate
    ? `<span class="sig CAND" title="符合买入条件，超出每日推荐名额">买入候选 #${s.buy_rank}</span>`
    : `<span class="sig ${s.signal}"${s.since ? ` title="信号自 ${s.since} 起${s.since_capped ? "（回测近一年内一直未变，实际更久）" : ""}"` : ""}>${LABEL[s.signal]}${
      s.signal !== "HOLD" && s.signal_days ? `<span class="days">${s.since_capped ? "≥" : ""}${s.signal_days}天</span>` : ""}</span>`;
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

  // 图表库加载失败时只隐藏图表、给出提示，不影响表格等其余部分
  function chartsAvailable() {
    if (window.echarts) return true;
    document.querySelectorAll(".chart").forEach((el) => {
      el.innerHTML = '<p class="dim" style="padding:16px">图表库加载失败，请刷新页面重试。</p>';
    });
    return false;
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
    renderIndustryOptions();
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
      ["买入候选", st.filter((x) => x.buy_candidate).length, ""],
      ["卖出信号", cnt("SELL"), "sell"],
      ["通过质量筛选", st.filter((x) => x.quality).length, ""],
      ["股息率中位数", pct(quantile(yields, 0.5)), ""],
    ];
    $("#kpis").innerHTML = items.map(([l, v, c]) => `<div class="kpi ${c}"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("");
  }

  // ---------- 表格 ----------
  // 行业下拉框：按成分股数量从多到少排列
  function renderIndustryOptions() {
    const counts = {};
    state.summary.stocks.forEach((s) => { const k = s.industry || "未知"; counts[k] = (counts[k] || 0) + 1; });
    $("#fIndustry").innerHTML = `<option value="">全部行业</option>` + Object.entries(counts)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh"))
      .map(([k, n]) => `<option value="${esc(k)}">${esc(k)}（${n}）</option>`).join("");
  }

  function bindFilters() {
    ["#fIndex", "#fIndustry", "#fSignal", "#fQuality", "#fSearch"].forEach((id) => $(id).addEventListener("input", () => { renderTable(); renderScatter(); }));
    document.querySelectorAll("#table th").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.sortKey === k) state.asc = !state.asc; else { state.sortKey = k; state.asc = k === "name" || k === "industry" || k === "pe" || k === "pe_pct"; }
      renderTable();
    }));
  }

  function filtered() {
    const idx = $("#fIndex").value, industry = $("#fIndustry").value, sig = $("#fSignal").value, q = $("#fSearch").value.trim(), quality = $("#fQuality").checked;
    return state.summary.stocks.filter((s) => {
      const codes = Object.keys(s.indices);
      if (idx === "both" && codes.length < 2) return false;
      if (idx && idx !== "both" && !codes.includes(idx)) return false;
      if (industry && s.industry !== industry) return false;
      if (sig === "CAND" ? !s.buy_candidate : sig && s.signal !== sig) return false;
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
        <td class="dim">${esc(s.industry || "—")}</td>
        <td>${sigBadge(s)}</td>
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
    if (!state.summary || !chartsAvailable()) return;
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
    $("#dTitle").innerHTML = `${esc(s.name)} <span class="code">${s.code}</span> <span class="tag">${esc(s.industry || "—")}</span> ${sigBadge(s)}`;
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
    if (!d || $("#detail").hidden || !chartsAvailable()) return;
    const rules = state.summary.rules;
    const accent = css("--accent"), muted = css("--muted"), buy = css("--buy"), sell = css("--sell");
    const yieldPct = d.yield.map((v) => (v == null ? null : +(v * 100).toFixed(2)));
    const zoom = [{ type: "inside", start: 0, end: 100 }];

    renderGrid(d);

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

  // ---------- 网格法买卖价位 ----------
  function renderGrid(d) {
    const g = d.grid;
    if (!g) { $("#gridTable").innerHTML = '<p class="dim">暂无网格数据</p>'; $("#gridNote").textContent = ""; return; }
    const s = state.summary.stocks.find((x) => x.code === state.current) || {};
    const buy = css("--buy"), sell = css("--sell"), muted = css("--muted"), accent = css("--accent");
    const n = Math.min(245, d.dates.length);
    const close = d.close.slice(-n);
    const prices = [...g.buy.map((x) => x.price), ...g.sell.map((x) => x.price), ...close.filter((v) => v != null)];
    const lo = Math.min(...prices), hi = Math.max(...prices), pad = (hi - lo) * 0.06;
    const line = (name, price, color, type) => ({
      name, yAxis: price, lineStyle: { color, type, width: 1 },
      label: { formatter: `${name} ${price.toFixed(2)}`, position: "insideEndTop", color, fontSize: 10 },
    });
    const anchorVisible = g.anchor >= lo - pad && g.anchor <= hi + pad;
    const o = baseOption();
    o.legend.show = false;
    o.grid = { left: 48, right: 16, top: 16, bottom: 30 };
    o.xAxis.data = d.dates.slice(-n);
    Object.assign(o.yAxis, { min: +(lo - pad).toFixed(2), max: +(hi + pad).toFixed(2), scale: false });
    Object.assign(o.yAxis.axisLabel, { showMinLabel: false, showMaxLabel: false });
    o.series = [{
      name: "收盘价", type: "line", data: close, showSymbol: false, lineStyle: { width: 1.5, color: accent }, itemStyle: { color: accent },
      markLine: {
        symbol: "none", silent: true,
        data: [
          ...(anchorVisible ? [line("中枢", g.anchor, muted, "solid")] : []),
          ...g.buy.map((x) => line(`买${x.level}`, x.price, buy, "dashed")),
          ...g.sell.map((x) => line(`卖${x.level}`, x.price, sell, "dashed")),
        ],
      },
    }];
    chart("cGrid").setOption(o, true);

    state.grid = g;
    renderGridTable();

    const pos = g.position;
    const where = Math.abs(pos) < 0.5 ? `现价在中枢（${g.anchor.toFixed(2)}）附近`
      : `现价在中枢（${g.anchor.toFixed(2)}）${pos < 0 ? "下方" : "上方"}约 ${Math.abs(pos).toFixed(1)} 档，处于${pos < 0 ? "低估" : "高估"}区`;
    const warn = s.signal === "SELL"
      ? `<br><span class="warn">⚠ 当前为卖出信号（${esc((s.reasons || [])[0] || "")}），不建议按网格买入。</span>` : "";
    $("#gridNote").innerHTML = `中枢 = ${esc(g.basis)}，每档间距 ${(g.step * 100).toFixed(0)}%（等比）。${where}。`
      + `用法（底仓 + 网格）：现价先买底仓 ${(g.core * 100).toFixed(0)}%，跌到买入档按金字塔加仓（越跌买得越多，买完即满仓）；`
      + `涨到卖出档时，低估区只卖出一份网格仓位、保留底仓，中枢及高估区每档减仓。仓位均为占计划投入总额的比例。${warn}`;
  }

  // 价位表：卖出档（远→近）、现价（底仓）、买入档（近→远）；填了投入总额时按 1 手 = 100 股算出每档股数
  function renderGridTable() {
    const g = state.grid;
    if (!g) return;
    const s = state.summary.stocks.find((x) => x.code === state.current) || {};
    const capital = Math.max(0, parseFloat($("#gridCapital").value) || 0);
    const noBuy = s.signal === "SELL"; // 卖出信号：不建底仓、不按网格买入
    const pct = (v) => (v == null ? "—" : (v * 100).toFixed(2) + "%");
    const gap = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`);
    const money = (v) => "¥" + Math.round(v).toLocaleString("zh-CN");
    const zone = (z) => (z === "低估" ? '<span class="lv-buy">低估区</span>' : z === "高估" ? '<span class="lv-sell">高估区</span>' : "中枢");
    let buyShares = 0, buyAmount = 0;
    const size = (side, p, price) => {
      if (side === "buy" && noBuy) return { pos: "暂停", qty: "—" };
      const v = Math.round(p * 1000) / 10; // 避免 0.06 * 100 = 6.000000000000001
      const pos = `${side === "buy" ? "买" : "卖"} ${v % 1 ? v.toFixed(1) : v}%`;
      if (!capital) return { pos, qty: "—" };
      const shares = Math.floor((capital * p) / price / 100) * 100;
      if (side === "buy") { buyShares += shares; buyAmount += shares * price; }
      return { pos, qty: shares ? `${shares.toLocaleString("zh-CN")} 股<span class="sub">${money(shares * price)}</span>` : "不足 1 手" };
    };
    const row = (label, cls, x, side) => {
      const z = size(side, x.pct, x.price);
      return `<tr><td class="${cls}">${label}</td><td class="num">${x.price.toFixed(2)}</td><td class="num">${pct(x.yield)}</td><td class="num">${gap(x.gap)}</td><td>${zone(x.zone)}</td><td class="num ${cls}">${z.pos}</td><td class="num">${z.qty}</td></tr>`;
    };
    // 卖出信号（基本面或估值风险）：不保留底仓，各卖出档平均分批清仓
    const sells = g.sell.slice().reverse()
      .map((x) => row(`卖出 ${x.level}`, "lv-sell", noBuy ? { ...x, pct: 1 / g.sell.length } : x, "sell")).join("");
    const core = size("buy", g.core, g.price);
    const nowZone = g.position < -0.5 ? "低估区" : g.position > 0.5 ? "高估区" : "中枢附近";
    const nowRow = `<tr class="now"><td>现价·底仓</td><td class="num">${g.price.toFixed(2)}</td><td class="num">${pct(s.yield)}</td><td class="num">—</td><td>${nowZone}</td><td class="num lv-buy">${core.pos}</td><td class="num">${core.qty}</td></tr>`;
    const buys = g.buy.map((x) => row(`买入 ${x.level}`, "lv-buy", x, "buy")).join("");
    const total = noBuy ? `卖出信号：暂停买入；持仓不保留底仓，在 ${g.sell.length} 个卖出档平均分批清仓`
      : capital ? `底仓 + ${g.buy.length} 档全部买入：${buyShares.toLocaleString("zh-CN")} 股，约 ${money(buyAmount)}（满仓）`
      : "填写投入总额后，按 1 手 = 100 股计算每档股数";
    $("#gridTable").innerHTML = `<table class="grid-table"><thead><tr><th>档位</th><th class="num">价格</th><th class="num">对应股息率</th><th class="num">距现价</th><th>估值区</th><th class="num">仓位</th><th class="num">股数 / 金额</th></tr></thead>
      <tbody>${sells}${nowRow}${buys}</tbody><tfoot><tr><td colspan="7">${total}</td></tr></tfoot></table>`;
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
      <p><b class="up">买入</b>：通过质量筛选，且股息率 ≥ ${(r.buy.min_yield * 100).toFixed(1)}%、股息率处于近 ${n} 年 ≥ ${r.buy.min_yield_percentile}% 分位、PE 分位 ≤ ${r.buy.max_pe_percentile}%。每天最多推荐 ${r.buy.max_daily ?? 5} 只（按综合评分取前 ${r.buy.max_daily ?? 5} 名），其余符合条件的标为“买入候选”，不发提醒</p>
      <p><b class="down">卖出</b>（任一）：股息率 ≤ ${(r.sell.max_yield * 100).toFixed(1)}%；股息率处于近 ${n} 年 ≤ ${r.sell.max_yield_percentile}% 分位；PE 为负；年度分红较上年削减超过 ${((1 - r.sell.dividend_cut_ratio) * 100).toFixed(0)}%；被调出指数</p>
      <p class="dim">阈值可在仓库的 config.yaml 中修改。</p>`;
  }

  try { $("#gridCapital").value = localStorage.getItem("gridCapital") || ""; } catch (e) { /* 忽略 */ }
  $("#gridCapital").addEventListener("input", () => {
    try { localStorage.setItem("gridCapital", $("#gridCapital").value); } catch (e) { /* 忽略 */ }
    renderGridTable();
  });

  addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
  initTheme();
  load();
})();
