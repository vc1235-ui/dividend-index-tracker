import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from datetime import date

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


class FlakyDemoProvider(DemoProvider):
    """每次请求固定耗时，且指定股票第一次请求失败，用来测试并发与补拉。"""

    def __init__(self, delay, fail_once=(), **kw):
        super().__init__(**kw)
        self.delay, self.fail_once = delay, set(fail_once)

    def valuation(self, code):
        import time

        time.sleep(self.delay)
        if code in self.fail_once:
            self.fail_once.discard(code)
            raise ConnectionError("模拟网络错误")
        return super().valuation(code)


def test_concurrent_fetch_with_retry(tmp_path):
    import time

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg["request_interval"] = 0

    def timed_run(workers, name):
        _mark_backfilled(tmp_path / f"{name}-state")
        t0 = time.monotonic()
        provider = FlakyDemoProvider(0.1, fail_once={"600000", "000042"}, n=12, end="2026-10-23")
        res = main.run(provider, {**cfg, "workers": workers}, tmp_path / name, tmp_path / f"{name}-state", send=False)
        return res, time.monotonic() - t0

    serial, t_serial = timed_run(1, "serial")
    parallel, t_parallel = timed_run(4, "parallel")

    assert t_parallel < t_serial * 0.5
    assert len(serial["stocks"]) == len(parallel["stocks"]) == 12  # 两只首次失败的股票都已补拉回来
    assert [(s["code"], s["signal"], s["score"]) for s in parallel["stocks"]] == [
        (s["code"], s["signal"], s["score"]) for s in serial["stocks"]
    ]


class CountingProvider(DemoProvider):
    """可设定“今天”的演示数据源，并统计全量 / 增量请求次数。"""

    def __init__(self, cutoff, broken_incremental=False, **kw):
        super().__init__(**kw)
        self.cutoff, self.broken = cutoff, broken_incremental
        self.full_calls, self.incremental_calls = 0, 0

    def _upto(self, code):
        df = DemoProvider.valuation(self, code)
        return df[df["date"] <= self.cutoff].reset_index(drop=True)

    def valuation(self, code):
        self.full_calls += 1
        return self._upto(code)

    def valuation_since(self, codes, since):
        self.incremental_calls += 1
        if self.broken:  # 模拟增量接口静默返回空结果
            return {}
        return {c: df[df["date"] > since].reset_index(drop=True) for c in codes if not (df := self._upto(c)).empty}


def _snapshot(res):
    return [(s["code"], s["date"], s["price"], s["yield"], s["pe_pct"], s["signal"]) for s in res["stocks"]]


def test_incremental_update(tmp_path):
    from tracker.cache import HistoryCache

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg.update(request_interval=0, workers=4)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=3)
    cache = HistoryCache(tmp_path / "cache", cfg["history_years"])

    # 第 1 天：没有缓存，全量拉取并写入缓存
    p1 = CountingProvider(days[0], n=10)
    main.run(p1, cfg, tmp_path / "w1", tmp_path / "s1", send=False, cache=cache)
    assert p1.full_calls == 10 and p1.incremental_calls == 0

    # 第 3 天：缓存差 2 个交易日，一次批量增量 + 1 只核对用的全量
    p3 = CountingProvider(days[2], n=10)
    inc = main.run(p3, cfg, tmp_path / "w3", tmp_path / "s3", send=False,
                   cache=HistoryCache(tmp_path / "cache", cfg["history_years"]))
    assert p3.incremental_calls == 1 and p3.full_calls == 1

    # 与不用缓存、直接全量拉取的结果完全一致
    full = main.run(CountingProvider(days[2], n=10), cfg, tmp_path / "wf", tmp_path / "sf", send=False)
    assert _snapshot(inc) == _snapshot(full)
    assert inc["data_date"] == days[2].strftime("%Y-%m-%d")


def test_incremental_falls_back_when_incomplete(tmp_path):
    from tracker.cache import HistoryCache

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg.update(request_interval=0, workers=4)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=2)
    main.run(CountingProvider(days[0], n=8), cfg, tmp_path / "w1", tmp_path / "s1", send=False,
             cache=HistoryCache(tmp_path / "cache", cfg["history_years"]))

    broken = CountingProvider(days[1], broken_incremental=True, n=8)
    res = main.run(broken, cfg, tmp_path / "w2", tmp_path / "s2", send=False,
                   cache=HistoryCache(tmp_path / "cache", cfg["history_years"]))
    assert broken.full_calls == 1 + 8  # 核对发现缺数据 → 全部改为全量
    assert res["data_date"] == days[1].strftime("%Y-%m-%d")

    # 超过全量刷新周期时，每次只轮流全量刷新 full_refresh_per_run 只，其余仍走增量
    stale = CountingProvider(days[1], n=8)
    main.run(stale, {**cfg, "full_refresh_days": 0, "full_refresh_per_run": 3}, tmp_path / "w3", tmp_path / "s3",
             send=False, cache=HistoryCache(tmp_path / "cache", cfg["history_years"]))
    assert stale.incremental_calls == 1 and stale.full_calls == 3 + 1  # 3 只轮换 + 1 只核对


def test_eastmoney_incremental_parsing(monkeypatch):
    import requests

    from tracker.fetch import AkshareProvider

    def row(code, d, close):
        return {"SECURITY_CODE": code, "TRADE_DATE": f"{d} 00:00:00", "CLOSE_PRICE": close, "PE_TTM": 6.1,
                "PB_MRQ": 0.6, "TOTAL_MARKET_CAP": 1e11, "OTHER": "x"}

    pages = {
        "1": {"result": {"pages": 2, "data": [row("600000", "2026-10-23", 10.1), row("601088", "2026-10-23", 40.0)]}},
        "2": {"result": {"pages": 2, "data": [row("600000", "2026-10-26", 10.3)]}},
    }
    seen = []

    class Resp:
        def __init__(self, js):
            self.js = js

        def raise_for_status(self):
            pass

        def json(self):
            return self.js

    def fake_get(url, params, timeout):
        seen.append(params)
        return Resp(pages[params["pageNumber"]])

    monkeypatch.setattr(requests, "get", fake_get)
    p = AkshareProvider.__new__(AkshareProvider)  # 不需要 akshare 本身
    out = p.valuation_since(["600000", "601088"], pd.Timestamp("2026-10-22"))
    assert seen[0]["filter"] == '(SECURITY_CODE in ("600000","601088"))(TRADE_DATE>\'2026-10-22\')'
    assert list(out["600000"]["close"]) == [10.1, 10.3]
    assert list(out["600000"].columns) == ["date", "close", "pe", "pb", "mcap"]
    assert out["601088"]["date"].iloc[0] == pd.Timestamp("2026-10-23")

    # 节假日没有新数据：接口返回 result 为空并提示“返回数据为空”
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: Resp({"result": None, "message": "返回数据为空"}))
    assert p.valuation_since(["600000"], pd.Timestamp("2026-10-26")) == {}
    # 其他错误要抛出，由调用方退回全量拉取
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: Resp({"result": None, "message": "参数错误"}))
    with pytest.raises(ValueError):
        p.valuation_since(["600000"], pd.Timestamp("2026-10-26"))


def test_limit_buys_keeps_top_scores():
    stocks = [{"code": str(i), "signal": signals.BUY, "score": s, "yield": y, "reasons": ["r"]}
              for i, (s, y) in enumerate([(70, 0.05), (90, 0.06), (80, 0.07), (80, 0.08), (60, 0.09), (85, 0.05), (75, 0.06)])]
    stocks.append({"code": "x", "signal": signals.SELL, "score": 99, "yield": 0.01, "reasons": ["s"]})
    signals.limit_buys(stocks, 5)
    buys = [s["code"] for s in stocks if s["signal"] == signals.BUY]
    assert sorted(buys) == ["1", "2", "3", "5", "6"]  # 90, 85, 80(股息率高), 80, 75
    ranks = {s["code"]: s["buy_rank"] for s in stocks if "buy_rank" in s}
    assert ranks == {"1": 1, "5": 2, "3": 3, "2": 4, "6": 5, "0": 6, "4": 7}
    cut = {s["code"]: s for s in stocks if s.get("buy_candidate")}
    assert set(cut) == {"0", "4"} and all(s["signal"] == signals.HOLD for s in cut.values())
    assert cut["0"]["reasons"][0].startswith("符合买入条件，综合评分排名第 6")
    assert stocks[-1]["signal"] == signals.SELL and "buy_rank" not in stocks[-1]


def test_industry_cached_and_buy_cap_in_pipeline(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg["rules"]["buy"].update(max_daily=1, min_yield=0.0, min_yield_percentile=0, max_pe_percentile=100)
    cfg["rules"]["sell"].update(max_yield=-1, max_yield_percentile=-1)
    cfg["rules"]["quality"]["min_score"] = 0

    class P(DemoProvider):
        calls = 0

        def industries(self, codes):
            P.calls += len(codes)
            return super().industries(codes)

    res = main.run(P(n=12, end="2026-10-23"), cfg, tmp_path / "w", tmp_path / "s", send=False, demo=True)
    assert sum(s["signal"] == "BUY" for s in res["stocks"]) == 1
    assert all(s["industry"] and s["industry"] != "未知" for s in res["stocks"])
    first = P.calls
    main.run(P(n=12, end="2026-10-23"), cfg, tmp_path / "w", tmp_path / "s", send=False, demo=True)
    assert first == 12 and P.calls == 12  # 第二次运行直接用缓存


def test_eastmoney_industries_batch(monkeypatch):
    import requests

    from tracker.fetch import AkshareProvider

    class Resp:
        def __init__(self, js):
            self.js = js

        def raise_for_status(self):
            pass

        def json(self):
            return self.js

    data = [
        {"SECURITY_CODE": "600000", "TRADE_DATE": "2026-10-23 00:00:00", "BOARD_NAME": "银行"},
        {"SECURITY_CODE": "601088", "TRADE_DATE": "2026-10-23 00:00:00", "BOARD_NAME": "-"},
        {"SECURITY_CODE": "601088", "TRADE_DATE": "2026-10-26 00:00:00", "BOARD_NAME": "煤炭行业"},
    ]
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: Resp({"result": {"pages": 1, "data": data}}))
    p = AkshareProvider.__new__(AkshareProvider)
    assert p.industries(["600000", "601088"]) == {"600000": "银行", "601088": "煤炭行业"}

    no_field = [{"SECURITY_CODE": "600000", "TRADE_DATE": "2026-10-23", "CLOSE_PRICE": 1}]
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: Resp({"result": {"pages": 1, "data": no_field}}))
    with pytest.raises(ValueError, match="可用字段: SECURITY_CODE, TRADE_DATE, CLOSE_PRICE"):
        p.industries(["600000"])


def test_industry_failure_is_fast_and_retried_next_run(tmp_path):
    import time

    class Broken(DemoProvider):
        def industries(self, codes):
            raise ConnectionError("blocked")

    t0 = time.monotonic()
    got = main.load_industries(Broken(n=5), ["600000", "000042"], tmp_path / "industries.json")
    assert got == {"600000": "未知", "000042": "未知"}
    assert time.monotonic() - t0 < 3  # 只重试一次、等待 2 秒
    assert not (tmp_path / "industries.json").exists()  # 失败不写缓存，下次运行会再试
    assert main.load_industries(DemoProvider(n=5), ["600000"], tmp_path / "industries.json") == {"600000": "公用事业"}


def test_industry_display_strips_level_suffix(tmp_path):
    import json

    (tmp_path / "industries.json").write_text(json.dumps(
        {"601398": {"industry": "银行Ⅱ", "updated": date.today().isoformat()}}, ensure_ascii=False), "utf-8")
    assert main.load_industries(DemoProvider(n=1), ["601398"], tmp_path / "industries.json") == {"601398": "银行"}


def test_grid_levels():
    close = np.full(100, 20.0)
    close[-1] = 18.0
    yld = np.full(100, 0.05)  # 每股分红 1 元，股息率中位数 5% → 中枢 20 元
    g = indicators.grid_levels(close, yld, 1.0, 0.05, 3)
    assert g["anchor"] == 20.0 and "5.00%" in g["basis"]
    assert -2.2 < g["position"] < -2.1  # 18 元在中枢下方约 2.16 档
    # 买入：现价下方最近 3 档；卖出：现价上方最近 3 档（含中枢本身）
    assert [x["price"] for x in g["buy"]] == [17.28, 16.45, 15.67]
    assert [x["price"] for x in g["sell"]] == [18.14, 19.05, 20.0]
    assert [x["zone"] for x in g["sell"]] == ["低估", "低估", "中枢"]
    assert [x["level"] for x in g["buy"]] == [1, 2, 3]
    assert g["buy"][0]["yield"] == pytest.approx(1 / 17.277, abs=1e-4)
    assert g["sell"][0]["gap"] == pytest.approx(18.14 / 18 - 1, abs=1e-3)
    # 仓位：底仓 40%，其余 60% 按 1:2:3 分给 3 个买入档，买完正好满仓
    assert g["core"] == 0.4 and [x["pct"] for x in g["buy"]] == [0.1, 0.2, 0.3]
    assert g["core"] + sum(x["pct"] for x in g["buy"]) == pytest.approx(1.0)
    # 卖出：低估区每档卖一份网格仓位 60%/3 = 20%，中枢及以上每档 25%
    assert [x["pct"] for x in g["sell"]] == [0.2, 0.2, 0.25]

    # 现价远离中枢时，档位仍然紧贴现价
    far = indicators.grid_levels(np.r_[np.full(99, 20.0), 9.0], yld, 1.0, 0.05, 2)
    assert far["position"] < -16
    assert 9.0 > far["buy"][0]["price"] > 9.0 / 1.05 - 0.01 and 9.0 < far["sell"][0]["price"] < 9.0 * 1.05 + 0.01

    # 现价正好在网格线上：该线不算买也不算卖
    on = indicators.grid_levels(np.r_[np.full(99, 20.0), 21.0], yld, 1.0, 0.05, 1)
    assert on["buy"][0]["price"] == 20.0 and on["sell"][0]["price"] == 22.05

    g0 = indicators.grid_levels(np.linspace(10, 12, 300), np.zeros(300), 0.0, 0.05, 2)  # 无分红 → 收盘价中位数
    assert "收盘价中位数" in g0["basis"] and g0["buy"][0]["yield"] is None


def _mark_backfilled(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps({"backfill_version": main.BACKFILL_VERSION}), "utf-8")


def test_signal_days_counts_trading_days(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg.update(request_interval=0)
    _mark_backfilled(tmp_path / "s")
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=3)
    first = main.run(CountingProvider(days[0], n=10), cfg, tmp_path / "w", tmp_path / "s", send=False)
    assert all(s["signal_days"] == 1 for s in first["stocks"])
    before = {s["code"]: s["signal"] for s in first["stocks"]}
    third = main.run(CountingProvider(days[2], n=10), cfg, tmp_path / "w", tmp_path / "s", send=False)
    for s in third["stocks"]:
        assert s["signal_days"] == (3 if before[s["code"]] == s["signal"] else 1)
    assert "grid" in json.loads((tmp_path / "w" / "data" / "stocks" / f"{s['code']}.json").read_text("utf-8"))


def test_backtest_last_day_matches_live_signal():
    from tracker import backtest

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    p = DemoProvider(n=12, end="2026-10-23")
    for code in p.codes:
        val, div = p.valuation(code), p.dividends(code)
        hist = backtest.daily_signals(val, div, cfg["rules"], 5, 0.7)
        m, _ = indicators.compute(val, div, 5, 0.7)
        m["score"] = signals.score(m)["total"]
        assert len(hist) == backtest.HORIZON
        assert hist["signal"].iloc[-1] == signals.evaluate(m, cfg["rules"])[0]
        assert hist["score"].iloc[-1] == pytest.approx(m["score"])


def test_backtest_daily_cap_and_streaks():
    from tracker import backtest

    d = ["2026-10-21", "2026-10-22", "2026-10-23"]
    h = {
        "A": pd.DataFrame({"date": d, "signal": ["BUY", "BUY", "BUY"], "score": [90, 90, 90], "yield": [.06] * 3}),
        "B": pd.DataFrame({"date": d, "signal": ["BUY", "BUY", "BUY"], "score": [80, 95, 95], "yield": [.06] * 3}),
        "C": pd.DataFrame({"date": d, "signal": ["SELL", "HOLD", "SELL"], "score": [10] * 3, "yield": [.01] * 3}),
    }
    backtest.apply_daily_cap(h, 1)  # 每天只留评分最高的 1 只
    assert list(h["A"]["signal"]) == ["BUY", "HOLD", "HOLD"]
    assert list(h["B"]["signal"]) == ["HOLD", "BUY", "BUY"]
    got = backtest.signal_streaks(h, {"A": "HOLD", "B": "BUY", "C": "SELL", "D": "BUY"})
    assert got == {"A": ("2026-10-22", False), "B": ("2026-10-22", False), "C": ("2026-10-23", False)}
    assert backtest.signal_streaks({"E": h["B"].iloc[1:]}, {"E": "BUY"}) == {"E": ("2026-10-22", True)}  # 到回测起点


def test_backfill_runs_once_and_sets_since(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg.update(request_interval=0)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=3)
    res = main.run(CountingProvider(days[0], n=10), cfg, tmp_path / "w", tmp_path / "s", send=False)
    state = json.loads((tmp_path / "s" / "state.json").read_text("utf-8"))
    assert state["backfill_version"] == main.BACKFILL_VERSION
    days_ = [s["signal_days"] for s in res["stocks"]]
    assert max(days_) > 1 and all(1 <= d <= 245 for d in days_)  # 回测补出了多日
    for s in res["stocks"]:
        assert state["stocks"][s["code"]]["since"] == s["since"]
        assert s["since_capped"] == (s["signal_days"] == 245)

    # 第二次运行不再回测，天数在状态基础上累加
    again = main.run(CountingProvider(days[2], n=10), cfg, tmp_path / "w", tmp_path / "s", send=False)
    before = {s["code"]: (s["signal"], s["signal_days"]) for s in res["stocks"]}
    for s in again["stocks"]:
        sig, n = before[s["code"]]
        assert s["signal_days"] == (n + 2 if s["signal"] == sig else 1)


def test_constituent_changes_detected_and_notified(tmp_path, monkeypatch):
    from tracker import notify

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text("utf-8"))
    cfg.update(request_interval=0)
    _mark_backfilled(tmp_path / "s")
    first = main.run(DemoProvider(n=12, end="2026-10-23"), cfg, tmp_path / "w", tmp_path / "s", send=False)
    assert first["member_changes"] == []  # 第一次没有可比较的旧名单

    class Adjusted(DemoProvider):  # 中证红利调入 1 只新股、调出 1 只
        def constituents(self, index_code):
            df = super().constituents(index_code)
            if index_code == "000922":
                df = df[df["code"] != self.codes[-1]]
                df.loc[len(df)] = {"code": "601999", "name": "新调入股", "weight": 1.0}
            return df

    sent = []
    monkeypatch.setattr(notify, "dispatch", lambda alerts, d, c, url, changes=None: sent.append((alerts, changes)))
    res = main.run(Adjusted(n=12, end="2026-10-23"), cfg, tmp_path / "w", tmp_path / "s", send=True)
    (chg,) = res["member_changes"]
    assert chg["index_name"] == "中证红利"
    assert chg["added"] == [{"code": "601999", "name": "新调入股"}]
    assert [x["code"] for x in chg["removed"]] == [DemoProvider(n=12).codes[-1]]
    assert sent and sent[0][1] == res["member_changes"]  # 同一数据日期没有新信号，但成分股调整照样通知
    summary = json.loads((tmp_path / "w" / "data" / "summary.json").read_text("utf-8"))
    assert summary["constituent_changes"][0]["added"][0]["code"] == "601999"


def test_message_only_new_signals_and_changes():
    from tracker import notify

    changes = [{"index_name": "上证红利", "added": [{"code": "600000", "name": "浦发银行"}], "removed": []}]
    title, body = notify.build_message([], "2026-10-27", None, changes)
    assert title == "红利指数提醒 2026-10-27：成分股调整"
    assert "上证红利：调入 浦发银行（600000）；调出 无" in body
    title, _ = notify.build_message([{"code": "1", "name": "A", "signal": "BUY", "reasons": []}], "2026-10-27", None)
    assert title == "红利指数提醒 2026-10-27：新买入 1 / 新卖出 0"


def test_email_is_sent_as_plain_text(monkeypatch):
    import email
    import smtplib
    from email.header import decode_header, make_header

    from tracker import notify

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, user, password):
            sent["login"] = (user, password)

        def sendmail(self, from_addr, to_addrs, msg):
            sent.update(from_addr=from_addr, to=to_addrs, msg=email.message_from_string(msg))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.qq.com", "SMTP_PORT": "", "SMTP_USER": "me@qq.com", "SMTP_PASSWORD": "code",
                 "MAIL_TO": ""}.items():
        monkeypatch.setenv(k, v)
    title, body = notify.build_message([{"code": "600000", "name": "浦发银行", "signal": "BUY", "price": 10.1,
                                         "yield": 0.052, "score": 78, "reasons": ["股息率 5.20% ≥ 5%"]}],
                                       "2026-10-27", "https://x/")
    notify.send_email(title, body)

    assert sent["server"] == ("smtp.qq.com", 465) and sent["login"] == ("me@qq.com", "code")
    assert sent["to"] == ["me@qq.com"]  # 未设 MAIL_TO 时发给自己
    msg = sent["msg"]
    assert str(make_header(decode_header(msg["Subject"]))) == "红利指数提醒 2026-10-27：新买入 1 / 新卖出 0"
    assert "me@qq.com" in msg["From"]
    text = msg.get_payload(decode=True).decode("utf-8")
    assert "浦发银行（600000）" in text and "查看图表：https://x/" in text
    assert "##" not in text and "**" not in text
