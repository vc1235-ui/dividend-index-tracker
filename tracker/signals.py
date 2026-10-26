"""综合评分与买卖信号规则。"""

from __future__ import annotations

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"
LABELS = {BUY: "买入", SELL: "卖出", HOLD: "持有/观望"}


def _lin(x: float, lo: float, hi: float) -> float:
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def score(m: dict) -> dict:
    """0-100 综合评分：股息率 30 + 分红稳定性 20 + 支付率 15 + 估值 20 + 分红增长 15。"""
    y = m.get("yield") or 0.0
    parts = {
        "yield": 30 * _lin(y, 0.03, 0.07),
        "stability": 20 * _lin(m.get("years") or 0, 0, 10),
    }
    p = m.get("payout")
    if p is None:
        parts["payout"] = 7.5
    elif p <= 0.3:
        parts["payout"] = 15 * _lin(p, 0.1, 0.3)
    elif p <= 0.7:
        parts["payout"] = 15.0
    else:
        parts["payout"] = 15 * (1 - _lin(p, 0.7, 1.0))
    val = [m["yield_pct"]] if m.get("yield_pct") is not None else []
    if m.get("pe_pct") is not None:
        val.append(100 - m["pe_pct"])
    parts["valuation"] = 20 * (sum(val) / len(val) / 100) if val else 10.0
    g = m.get("dps_cagr")
    parts["growth"] = 7.5 if g is None else 15 * _lin(g, -0.1, 0.1)
    total = round(sum(parts.values()), 1)
    return {"total": total, "parts": {k: round(v, 1) for k, v in parts.items()}}


def evaluate(m: dict, rules: dict) -> tuple[str, list[str], bool]:
    """返回 (信号, 理由列表, 是否通过质量筛选)。卖出优先于买入。"""
    q, b, s = rules["quality"], rules["buy"], rules["sell"]
    y, ypct, pe, pe_pct = m.get("yield"), m.get("yield_pct"), m.get("pe"), m.get("pe_pct")

    sell = []
    if y is not None and y <= s["max_yield"]:
        sell.append(f"股息率 {y:.2%} ≤ {s['max_yield']:.0%}")
    if ypct is not None and ypct <= s["max_yield_percentile"]:
        sell.append(f"股息率处于近年 {ypct:.1f}% 分位（≤{s['max_yield_percentile']}%，估值偏高）")
    if pe is not None and pe <= 0:
        sell.append("PE(TTM) 为负，公司亏损")
    if m.get("dividend_cut"):
        sell.append(f"{m.get('latest_year')} 年度分红较上年削减超过 {1 - s['dividend_cut_ratio']:.0%}")

    quality = (
        (m.get("years") or 0) >= q["min_consecutive_years"]
        and (pe is None or pe > 0)
        and (m.get("payout") is None or m["payout"] <= q["max_payout_ratio"])
        and m["score"] >= q["min_score"]
    )
    if sell:
        return SELL, sell, quality

    if quality and y is not None and ypct is not None:
        buy_ok = y >= b["min_yield"] and ypct >= b["min_yield_percentile"]
        pe_ok = pe_pct is None or pe_pct <= b["max_pe_percentile"]
        if buy_ok and pe_ok:
            reasons = [
                f"股息率 {y:.2%} ≥ {b['min_yield']:.0%}",
                f"股息率处于近年 {ypct:.1f}% 分位（≥{b['min_yield_percentile']}%）",
                f"连续分红 {m['years']} 年，综合评分 {m['score']:.0f}",
            ]
            if pe_pct is not None:
                reasons.append(f"PE 分位 {pe_pct:.0f}%（≤{b['max_pe_percentile']}%）")
            return BUY, reasons, quality

    reasons = []
    if not quality:
        reasons.append("未通过质量筛选")
    elif y is not None and y < b["min_yield"]:
        reasons.append(f"股息率 {y:.2%} 未达买入线 {b['min_yield']:.0%}")
    elif ypct is not None and ypct < b["min_yield_percentile"]:
        reasons.append(f"股息率分位 {ypct:.1f}% 未达 {b['min_yield_percentile']}%")
    elif pe_pct is not None and pe_pct > b["max_pe_percentile"]:
        reasons.append(f"PE 分位 {pe_pct:.0f}% 偏高")
    return HOLD, reasons, quality


def limit_buys(stocks: list[dict], max_buys: int) -> None:
    """同时满足买入条件的股票按综合评分（其次股息率）排序，只保留前 max_buys 只为买入。

    其余改为持有/观望并标记为买入候选（buy_candidate），不触发买入提醒。原地修改。
    """
    cands = sorted((s for s in stocks if s["signal"] == BUY), key=lambda s: (-s["score"], -(s.get("yield") or 0)))
    for rank, s in enumerate(cands, 1):
        s["buy_rank"] = rank
        if rank > max_buys:
            s["signal"] = HOLD
            s["buy_candidate"] = True
            s["reasons"] = [f"符合买入条件，综合评分排名第 {rank}（每日最多推荐 {max_buys} 只）"] + s["reasons"]
