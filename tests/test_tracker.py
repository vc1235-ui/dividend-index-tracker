from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from tracker import indicators, main, signals
from tracker.fetch import DemoProvider

ROOT = Path(__file__).resolve().parent.parent
RULES = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))["rules"]


def div_df(rows):
    df = pd.DataFrame(rows, columns=["report_date", "ex_date", "dps", "eps", "progress"])
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


DIV = div_df(
    [
        ("2021-12-31", "2022-06-20", 0.40, 1.0, "实施分配"),
        ("2022-12-31", "2023-06-20", 0.44, 1.0, "实施分配"),
        ("2023-06-30", "2023-10-10", 0.20, None, "实施分配"),
        ("2023-12-31", "2024-06-20", 0.30, 1.0, "实施分配"),
        ("2024-12-31", None, 0.55, 1.1, "股东大会决议通过"),
        ("2020-12-31", None, 0.0, 0.5, "不分配不转增"),
    ]
)


def test_ttm_dps_window():
    dates = pd.DatetimeIndex(["2023-06-19", "2023-06-20", "2023-12-01", "2024-06-19", "2024-06-20"])
    ttm = indicators.ttm_dps_series(dates, DIV)
    # 2023-06-19 窗口内只有 2022-06-20 那次
    assert ttm.tolist() == pytest.approx([0.40, 0.44, 0.64, 0.20, 0.50])


def test_annual_and_streak():
    rows = indicators.annual_dividends(DIV)
    by = {r["year"]: r for r in rows}
    assert by[2023]["dps"] == pytest.approx(0.50)  # 中期 + 年度
    assert by[2023]["payout"] == pytest.approx(0.50)
    assert by[2020]["dps"] == 0
    assert indicators.latest_complete_year(rows) == 2024
    assert indicators.consecutive_years(rows) == 4  # 2021-2024，2020 未分配
    assert indicators.dps_cagr(rows) == pytest.approx((0.55 / 0.40) ** (1 / 3) - 1)
    assert not indicators.dividend_cut(rows, 0.7)


def test_dividend_cut():
    rows = indicators.annual_dividends(DIV[DIV["report_date"].dt.year <= 2023])
    # 2023 年 0.50 vs 2022 年 0.44，未削减
    assert not indicators.dividend_cut(rows, 0.7)
    cut = div_df([("2022-12-31", "2023-06-01", 1.0, 2.0, "实施分配"), ("2023-12-31", "2024-06-01", 0.5, 2.0, "实施分配")])
    assert indicators.dividend_cut(indicators.annual_dividends(cut), 0.7)


def test_percentile_rank():
    assert indicators.percentile_rank(np.arange(100), 49) == pytest.approx(50)
    assert indicators.percentile_rank([1, 2], 1) is None  # 样本太少


def base_metrics(**kw):
    m = {"yield": 0.06, "yield_pct": 85, "pe": 6.0, "pe_pct": 20, "years": 10, "payout": 0.5, "dps_cagr": 0.05,
         "dividend_cut": False, "latest_year": 2024}
    m.update(kw)
    m["score"] = signals.score(m)["total"]
    return m


def test_buy_signal():
    sig, reasons, quality = signals.evaluate(base_metrics(), RULES)
    assert quality and sig == signals.BUY and reasons


def test_sell_overrides():
    assert signals.evaluate(base_metrics(**{"yield": 0.025}), RULES)[0] == signals.SELL
    assert signals.evaluate(base_metrics(pe=-3.0, pe_pct=None), RULES)[0] == signals.SELL
    assert signals.evaluate(base_metrics(dividend_cut=True), RULES)[0] == signals.SELL
    assert signals.evaluate(base_metrics(yield_pct=10), RULES)[0] == signals.SELL


def test_hold_when_not_cheap():
    sig, reasons, _ = signals.evaluate(base_metrics(yield_pct=50), RULES)
    assert sig == signals.HOLD and "分位" in reasons[0]


def test_score_bounds():
    hi = signals.score(base_metrics(**{"yield": 0.1, "yield_pct": 100, "pe_pct": 0, "dps_cagr": 0.2}))["total"]
    lo = signals.score({"yield": 0.0, "years": 0, "payout": 1.2, "yield_pct": 0, "pe_pct": 100, "dps_cagr": -0.3})["total"]
    assert hi == 100 and lo == 0


def test_diff_signals_transitions_and_cooldown():
    state = {"stocks": {"600000": {"name": "A", "signal": "HOLD", "since": "2024-01-01"},
                        "600001": {"name": "B", "signal": "BUY", "since": "2024-01-01"},
                        "600002": {"name": "C", "signal": "HOLD", "since": "2024-01-01"},
                        "600003": {"name": "D", "signal": "HOLD", "since": "2024-01-01"}}}
    ncfg = {"on_signals": ["BUY", "SELL"], "cooldown_days": 20}
    stocks = [
        {"code": "600000", "name": "A", "signal": "BUY", "reasons": ["x"]},
        {"code": "600001", "name": "B", "signal": "BUY", "reasons": ["x"]},
    ]
    # 600002 被调出指数，600003 本次数据获取失败但仍是成分股
    alerts = main.diff_signals(stocks, {"600000", "600001", "600003"}, state, "2024-05-06", ncfg)
    assert [(a["code"], a["signal"]) for a in alerts] == [("600000", "BUY"), ("600002", "SELL")]
    assert state["stocks"]["600003"]["signal"] == "HOLD"
    assert state["stocks"]["600002"]["removed"] == "2024-05-06"

    # 回落后 5 天内再次触发买入：冷却期内不重复提醒
    state["stocks"]["600000"]["signal"] = "HOLD"
    alerts = main.diff_signals(stocks, {"600000", "600001", "600003"}, state, "2024-05-10", ncfg)
    assert alerts == []
    assert "600002" not in state["stocks"]  # 已标记调出的记录被清理


def test_demo_pipeline(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    res = main.run(DemoProvider(n=12, end="2026-10-23"), cfg, tmp_path / "web", tmp_path / "state", send=False, demo=True)
    assert len(res["stocks"]) == 12
    assert (tmp_path / "web" / "data" / "summary.json").exists()
    assert len(list((tmp_path / "web" / "data" / "stocks").glob("*.json"))) == 12
    assert {s["signal"] for s in res["stocks"]} <= {"BUY", "SELL", "HOLD"}
