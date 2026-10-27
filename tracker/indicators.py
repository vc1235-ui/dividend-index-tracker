"""由行情、估值与分红记录计算指标。全部为纯函数，便于测试。"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# 方案进度中包含这些字样的记录不计入分红
EXCLUDED_PROGRESS = ("不分配", "不实施", "取消", "终止", "未通过")


def valid_plans(div: pd.DataFrame) -> pd.DataFrame:
    progress = div["progress"].fillna("").astype(str)
    mask = ~progress.str.contains("|".join(EXCLUDED_PROGRESS)) & (div["dps"] > 0)
    return div[mask]


def paid_events(div: pd.DataFrame) -> pd.DataFrame:
    """已实施（有除权除息日）的现金分红。"""
    return div[div["ex_date"].notna() & (div["dps"] > 0)].sort_values("ex_date")


def ttm_dps_series(dates: pd.DatetimeIndex, div: pd.DataFrame) -> np.ndarray:
    """每个交易日过去 365 天内除息的每股分红合计。"""
    out = np.zeros(len(dates))
    for ex, dps in zip(paid_events(div)["ex_date"], paid_events(div)["dps"]):
        out[(dates >= ex) & (dates < ex + pd.Timedelta(days=365))] += dps
    return out


def percentile_rank(values, current) -> float | None:
    """current 在 values 中的百分位（0-100，≤current 的比例）。"""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if current is None or not np.isfinite(current) or arr.size < 20:
        return None
    return float((arr <= current).mean() * 100)


def annual_dividends(div: pd.DataFrame) -> list[dict]:
    """按财年（报告期所属年度）汇总每股分红、年报每股收益和支付率。"""
    if div.empty:
        return []
    plans = valid_plans(div)
    dps = plans.groupby(plans["report_date"].dt.year)["dps"].sum()
    annual = div[div["report_date"].dt.month == 12].dropna(subset=["report_date"])
    eps = annual.groupby(annual["report_date"].dt.year)["eps"].first()
    years = sorted(set(dps.index) | set(annual["report_date"].dt.year))
    rows = []
    for y in years:
        d = float(dps.get(y, 0.0))
        e = eps.get(y)
        e = float(e) if e is not None and pd.notna(e) else None
        payout = d / e if e and e > 0 else None
        rows.append({"year": int(y), "dps": round(d, 4), "eps": e, "payout": payout, "complete": y in set(annual["report_date"].dt.year)})
    return rows


def latest_complete_year(rows: list[dict]) -> int | None:
    years = [r["year"] for r in rows if r["complete"]]
    return max(years) if years else None


def consecutive_years(rows: list[dict]) -> int:
    """截至最近一个已出年报方案的财年，连续现金分红的年数。"""
    latest = latest_complete_year(rows)
    if latest is None:
        return 0
    by_year = {r["year"]: r["dps"] for r in rows}
    n, y = 0, latest
    while by_year.get(y, 0) > 0:
        n += 1
        y -= 1
    return n


def dps_cagr(rows: list[dict], span: int = 3) -> float | None:
    latest = latest_complete_year(rows)
    if latest is None:
        return None
    by_year = {r["year"]: r["dps"] for r in rows}
    a, b = by_year.get(latest - span, 0), by_year.get(latest, 0)
    if a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / span) - 1


def dividend_cut(rows: list[dict], ratio: float) -> bool:
    latest = latest_complete_year(rows)
    if latest is None:
        return False
    by_year = {r["year"]: r["dps"] for r in rows}
    prev = by_year.get(latest - 1, 0)
    return prev > 0 and by_year.get(latest, 0) < prev * ratio


def _num(x, nd: int = 4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def grid_levels(close: np.ndarray, yld: np.ndarray, dps_ttm: float, step: float, levels: int,
                core: float = 0.4, sell_high: float = 0.25) -> dict:
    """网格法买卖价位（底仓 + 网格）。

    以“股息率回到近年中位数时的价格”为中枢，按 step 等比划分价格网格（中枢 × (1+step)^k）。
    取现价下方最近的 levels 档为买入价、上方最近的 levels 档为卖出价，保证每一档都可能被触及；
    每档标注所在估值区：中枢以下为低估区（卖出档只卖网格仓位、保留底仓），以上为高估区（可逐步减仓）。
    没有分红数据时退回用近一年收盘价中位数做中枢。

    仓位（占计划投入总额的比例）：现价买入底仓 core；其余 1 - core 按 1:2:…:levels 金字塔分给各买入档
    （越跌买得越多，买完即满仓）；卖出档在低估区每档卖出一份网格仓位 (1 - core) / levels、保留底仓，
    在中枢及高估区每档卖出 sell_high、逐步减仓。
    """
    price = float(close[-1])
    hist = yld[np.isfinite(yld) & (yld > 0)]
    if dps_ttm > 0 and hist.size >= 20:
        anchor_yield = float(np.median(hist))
        anchor = dps_ttm / anchor_yield
        basis = f"股息率回到近年中位数 {anchor_yield:.2%} 时的价格"
    else:
        anchor = float(np.median(close[-245:]))
        basis = "近一年收盘价中位数（无分红数据）"
    r = 1 + step
    position = math.log(price / anchor) / math.log(r)  # 现价相对中枢的档数，-2.3 表示在中枢下方 2.3 档

    grid_total = 1 - core
    weights = range(1, levels + 1)

    def level(n: int, k: int, pct: float) -> dict:
        p = anchor * r ** k
        return {"level": n, "k": k, "price": round(p, 2), "yield": _num(dps_ttm / p) if dps_ttm > 0 else None,
                "gap": _num(p / price - 1), "zone": "低估" if k < 0 else "高估" if k > 0 else "中枢", "pct": round(pct, 4)}

    below = math.ceil(position - 1e-9) - 1  # 现价下方最近一档
    above = math.floor(position + 1e-9) + 1  # 现价上方最近一档
    return {
        "anchor": round(anchor, 2),
        "basis": basis,
        "step": step,
        "price": round(price, 2),
        "position": _num(position, 2),
        "core": core,
        "buy": [level(i + 1, below - i, grid_total * w / sum(weights)) for i, w in enumerate(weights)],
        "sell": [level(i + 1, k, grid_total / levels if k < 0 else sell_high)
                 for i, k in enumerate(range(above, above + levels))],
    }


def compute(val: pd.DataFrame, div: pd.DataFrame, history_years: int, cut_ratio: float,
            grid: dict | None = None) -> tuple[dict, dict]:
    """返回 (当前指标, 用于画图的历史序列)。"""
    val = val.dropna(subset=["date", "close"]).sort_values("date")
    val = val[val["close"] > 0]
    if val.empty:
        raise ValueError("无行情数据")
    end = val["date"].iloc[-1]
    val = val[val["date"] > end - pd.DateOffset(years=history_years)].reset_index(drop=True)
    dates = pd.DatetimeIndex(val["date"])
    close = val["close"].to_numpy(float)
    ttm = ttm_dps_series(dates, div)
    yld = ttm / close
    pe = val["pe"].to_numpy(float)
    pb = val["pb"].to_numpy(float)

    rows = annual_dividends(div)
    latest_year = latest_complete_year(rows)
    latest_row = next((r for r in rows if r["year"] == latest_year), None)

    cur_pe = pe[-1] if np.isfinite(pe[-1]) else None
    cur_pb = pb[-1] if np.isfinite(pb[-1]) else None
    metrics = {
        "date": end.strftime("%Y-%m-%d"),
        "price": _num(close[-1], 3),
        "yield": _num(yld[-1]),
        "yield_pct": _num(percentile_rank(yld, yld[-1]), 1),
        "pe": _num(cur_pe, 2),
        "pe_pct": _num(percentile_rank(pe[pe > 0], cur_pe) if cur_pe and cur_pe > 0 else None, 1),
        "pb": _num(cur_pb, 3),
        "pb_pct": _num(percentile_rank(pb[pb > 0], cur_pb) if cur_pb and cur_pb > 0 else None, 1),
        "mcap": _num(val["mcap"].iloc[-1], 0),
        "dps_ttm": _num(ttm[-1]),
        "years": consecutive_years(rows),
        "payout": _num(latest_row["payout"]) if latest_row else None,
        "dps_cagr": _num(dps_cagr(rows)),
        "dividend_cut": dividend_cut(rows, cut_ratio),
        "latest_year": latest_year,
        "change_1y": _num(close[-1] / close[max(0, len(close) - 245)] - 1),
    }
    series = {
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "close": [_num(x, 3) for x in close],
        "yield": [_num(x) for x in yld],
        "pe": [_num(x, 2) for x in pe],
        "pb": [_num(x, 3) for x in pb],
        "annual": [
            {"year": r["year"], "dps": _num(r["dps"]), "eps": _num(r["eps"]), "payout": _num(r["payout"])}
            for r in rows
            if r["year"] >= (latest_year or end.year) - 9
        ],
        "events": [
            {"ex_date": e.strftime("%Y-%m-%d"), "dps": _num(d)}
            for e, d in zip(paid_events(div)["ex_date"], paid_events(div)["dps"])
            if e >= dates[0]
        ],
        "grid": grid_levels(close, yld, float(ttm[-1]), (grid or {}).get("step", 0.05), (grid or {}).get("levels", 4),
                            (grid or {}).get("core", 0.4), (grid or {}).get("sell_high", 0.25)),
    }
    return metrics, series
