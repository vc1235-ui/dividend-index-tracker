"""回测：按历史上每个交易日当时可得的数据重算信号，用于补充信号已持续的天数。

- 股息率只计已除息的分红；股息率 / PE 分位用当天之前 history_years 年的数据；
- 年度分红统计（连续年数、支付率、增速、是否削减）只计当时已披露的方案：
  年报约在报告期后 4 个月可得，中报 / 季报约 2 个月；
- 每日最多推荐买入只数也按天横向排名后截取，与实盘规则一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators, signals

HORIZON = 245  # 回测最近约 1 年的交易日（估值缓存保留 history_years + 1 年，前 history_years 年用于算分位）


def _disclosed_from(report_date: pd.Timestamp) -> pd.Timestamp:
    """分红方案大致可得的日期。"""
    return report_date + pd.Timedelta(days=120 if report_date.month == 12 else 62)


def daily_signals(val: pd.DataFrame, div: pd.DataFrame, rules: dict, history_years: int,
                  cut_ratio: float, days: int = HORIZON) -> pd.DataFrame:
    """最近 days 个交易日每天的规则信号（未做每日只数截取）：date, signal, score, yield。"""
    val = val.dropna(subset=["date", "close"]).sort_values("date")
    val = val[val["close"] > 0].reset_index(drop=True)
    dates = pd.DatetimeIndex(val["date"])
    close = val["close"].to_numpy(float)
    pe = val["pe"].to_numpy(float)
    yld = indicators.ttm_dps_series(dates, div) / close
    disclosed = div["report_date"].map(_disclosed_from).to_numpy() if not div.empty else np.array([])

    annual_cache: dict[tuple, dict] = {}
    out = []
    for i in range(max(0, len(val) - days), len(val)):
        t = dates[i]
        lo = dates.searchsorted(t - pd.DateOffset(years=history_years), side="right")
        visible = tuple(np.flatnonzero(disclosed <= np.datetime64(t)))
        if visible not in annual_cache:
            rows = indicators.annual_dividends(div.iloc[list(visible)])
            latest = indicators.latest_complete_year(rows)
            latest_row = next((r for r in rows if r["year"] == latest), None)
            annual_cache[visible] = {
                "years": indicators.consecutive_years(rows),
                "payout": indicators._num(latest_row["payout"]) if latest_row else None,
                "dps_cagr": indicators._num(indicators.dps_cagr(rows)),
                "dividend_cut": indicators.dividend_cut(rows, cut_ratio),
                "latest_year": latest,
            }
        cur_pe = pe[i] if np.isfinite(pe[i]) else None
        pe_win = pe[lo:i + 1]
        # 取整方式与 indicators.compute 一致，保证最后一天与实盘信号相同
        m = {
            "yield": indicators._num(yld[i]),
            "yield_pct": indicators._num(indicators.percentile_rank(yld[lo:i + 1], yld[i]), 1),
            "pe": indicators._num(cur_pe, 2),
            "pe_pct": indicators._num(indicators.percentile_rank(pe_win[pe_win > 0], cur_pe)
                                      if cur_pe and cur_pe > 0 else None, 1),
            **annual_cache[visible],
        }
        m["score"] = signals.score(m)["total"]
        out.append((t.strftime("%Y-%m-%d"), signals.evaluate(m, rules)[0], m["score"], m["yield"] or 0.0))
    return pd.DataFrame(out, columns=["date", "signal", "score", "yield"])


def apply_daily_cap(history: dict[str, pd.DataFrame], max_buys: int) -> None:
    """每天按综合评分（其次股息率）只保留前 max_buys 只买入，其余改为持有/观望。原地修改。"""
    if not history:
        return
    allrows = pd.concat([df.assign(code=c) for c, df in history.items()], ignore_index=True)
    buys = allrows[allrows["signal"] == signals.BUY].sort_values(["date", "score", "yield"], ascending=[True, False, False])
    over = buys[buys.groupby("date").cumcount() >= max_buys]
    for code, rows in over.groupby("code"):
        df = history[code]
        df.loc[df["date"].isin(rows["date"]), "signal"] = signals.HOLD


def signal_streaks(history: dict[str, pd.DataFrame], current: dict[str, str]) -> dict[str, tuple[str, bool]]:
    """当前信号往回连续保持的起始日期：code -> (起始日期, 是否已到回测起点)。

    回测最后一天的信号与实盘不一致的股票不返回（沿用状态记录）。
    """
    out = {}
    for code, df in history.items():
        sigs = df["signal"].to_numpy()
        if len(sigs) == 0 or sigs[-1] != current.get(code):
            continue
        j = len(sigs) - 1
        while j > 0 and sigs[j - 1] == sigs[-1]:
            j -= 1
        out[code] = (df["date"].iloc[j], j == 0)
    return out
