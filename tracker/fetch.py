"""数据获取层。

所有 provider 返回统一格式的 DataFrame：

- constituents(index_code) -> code, name, weight
- valuation(code)          -> date(datetime64), close, pe, pb, mcap   （不复权收盘价，完整历史）
- valuation_since(codes, since) -> {code: 同上}                         （可选：批量增量，只含 since 之后）
- dividends(code)          -> report_date, ex_date, dps, eps, progress （dps 为每股税前现金分红，元）
"""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

EASTMONEY_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _retry(fn, *args, tries: int = 3, wait: float = 3.0, **kwargs):
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # 数据源偶发失败，重试
            last = e
            log.warning("%s %s 第 %d/%d 次失败: %s", fn.__name__, kwargs or args, i + 1, tries, e)
            time.sleep(wait * (i + 1))
    raise last


class AkshareProvider:
    """基于 AKShare（中证指数官网 + 东方财富数据中心）的真实数据。"""

    def __init__(self):
        import akshare as ak

        self.ak = ak

    def constituents(self, index_code: str) -> pd.DataFrame:
        ak = self.ak
        try:
            df = _retry(ak.index_stock_cons_weight_csindex, symbol=index_code)
            return pd.DataFrame({"code": df["成分券代码"], "name": df["成分券名称"], "weight": df["权重"]})
        except Exception as e:
            log.warning("中证权重文件获取失败(%s)，改用成分股列表: %s", index_code, e)
        try:
            df = _retry(ak.index_stock_cons_csindex, symbol=index_code)
            return pd.DataFrame({"code": df["成分券代码"], "name": df["成分券名称"], "weight": np.nan})
        except Exception as e:
            log.warning("中证成分股列表获取失败(%s)，改用新浪: %s", index_code, e)
        df = _retry(ak.index_stock_cons, symbol=index_code)
        return pd.DataFrame(
            {"code": df["品种代码"].astype(str).str.zfill(6), "name": df["品种名称"], "weight": np.nan}
        )

    def valuation(self, code: str) -> pd.DataFrame:
        ak = self.ak
        try:
            df = _retry(ak.stock_value_em, symbol=code)
            out = pd.DataFrame(
                {
                    "date": pd.to_datetime(df["数据日期"]),
                    "close": df["当日收盘价"],
                    "pe": df["PE(TTM)"],
                    "pb": df["市净率"],
                    "mcap": df["总市值"],
                }
            )
            if not out.empty:
                return out.sort_values("date").reset_index(drop=True)
        except Exception as e:
            log.warning("估值数据获取失败(%s)，改用日线行情（无 PE/PB）: %s", code, e)
        df = _retry(ak.stock_zh_a_hist, symbol=code, period="daily", adjust="")
        return pd.DataFrame(
            {
                "date": pd.to_datetime(df["日期"]),
                "close": df["收盘"],
                "pe": np.nan,
                "pb": np.nan,
                "mcap": np.nan,
            }
        )

    def valuation_since(self, codes: list[str], since: pd.Timestamp) -> dict[str, pd.DataFrame]:
        """一次批量请求多只股票 since 之后（不含）的估值数据，用于增量更新。

        与 stock_value_em 是同一个东方财富接口，只是按股票列表和日期过滤，几天的数据只需一两页。
        """
        import requests

        codes_expr = ",".join(f'"{c}"' for c in codes)
        params = {
            "sortColumns": "TRADE_DATE",
            "sortTypes": "1",
            "pageSize": "500",
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
            "filter": f"(SECURITY_CODE in ({codes_expr}))(TRADE_DATE>'{since:%Y-%m-%d}')",
        }
        rows, page, pages = [], 1, 1
        while page <= pages:
            r = requests.get(EASTMONEY_DATACENTER, params={**params, "pageNumber": str(page)}, timeout=30)
            r.raise_for_status()
            js = r.json()
            result = js.get("result")
            if not result:
                if "空" in str(js.get("message", "")):  # 没有新数据（例如节假日）
                    break
                raise ValueError(f"东方财富接口返回异常: {js.get('message')}")
            rows.extend(result.get("data") or [])
            pages = int(result.get("pages") or 1)
            page += 1
        if not rows:
            return {}
        df = pd.DataFrame(rows)
        out = pd.DataFrame(
            {
                "code": df["SECURITY_CODE"].astype(str).str.zfill(6),
                "date": pd.to_datetime(df["TRADE_DATE"]).dt.normalize(),
                "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
                "pe": pd.to_numeric(df["PE_TTM"], errors="coerce"),
                "pb": pd.to_numeric(df["PB_MRQ"], errors="coerce"),
                "mcap": pd.to_numeric(df["TOTAL_MARKET_CAP"], errors="coerce"),
            }
        )
        return {c: g.drop(columns="code").sort_values("date").reset_index(drop=True) for c, g in out.groupby("code")}

    def dividends(self, code: str) -> pd.DataFrame:
        try:
            df = _retry(self.ak.stock_fhps_detail_em, symbol=code)
        except TypeError:  # 无任何分红记录时接口返回空结果
            df = pd.DataFrame()
        if df is None or df.empty:
            return empty_dividends()
        return pd.DataFrame(
            {
                "report_date": pd.to_datetime(df["报告期"], errors="coerce"),
                "ex_date": pd.to_datetime(df["除权除息日"], errors="coerce"),
                # 东方财富口径为“每 10 股派息（税前）”
                "dps": pd.to_numeric(df["现金分红-现金分红比例"], errors="coerce").fillna(0) / 10,
                "eps": pd.to_numeric(df["每股收益"], errors="coerce"),
                "progress": df["方案进度"].fillna("").astype(str),
            }
        )


def empty_dividends() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "report_date": pd.Series(dtype="datetime64[ns]"),
            "ex_date": pd.Series(dtype="datetime64[ns]"),
            "dps": pd.Series(dtype=float),
            "eps": pd.Series(dtype=float),
            "progress": pd.Series(dtype=str),
        }
    )


class DemoProvider:
    """离线演示数据：随机生成的股价、估值与分红，用于本地预览网页和测试。"""

    NAMES = [
        "示例银行", "示例煤业", "示例高速", "示例电力", "示例钢铁", "示例港口", "示例石化",
        "示例建材", "示例家电", "示例保险", "示例航运", "示例水务", "示例燃气", "示例纺织",
        "示例化工", "示例传媒", "示例地产", "示例证券", "示例有色", "示例食品",
    ]

    def __init__(self, n: int = 20, seed: int = 7, end: str | None = None):
        self.n = n
        self.seed = seed
        self.end = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
        codes = [f"{600000 + i * 37:06d}" if i % 2 == 0 else f"{1 + i * 41:06d}" for i in range(n)]
        self.codes = codes

    def _rng(self, code: str) -> np.random.Generator:
        return np.random.default_rng(self.seed * 1_000_003 + int(code))

    def constituents(self, index_code: str) -> pd.DataFrame:
        # 两个指数部分重叠
        if index_code == "000015":
            idx = list(range(0, self.n * 2 // 3))
        else:
            idx = list(range(self.n // 3, self.n))
        w = np.full(len(idx), 100 / len(idx))
        return pd.DataFrame(
            {"code": [self.codes[i] for i in idx], "name": [self.NAMES[i % len(self.NAMES)] for i in idx], "weight": w}
        )

    def valuation(self, code: str) -> pd.DataFrame:
        rng = self._rng(code)
        dates = pd.bdate_range(end=self.end, periods=252 * 6)
        drift = rng.normal(0.0001, 0.0002)
        rets = rng.normal(drift, 0.012, len(dates))
        close = rng.uniform(4, 30) * np.exp(np.cumsum(rets))
        eps = close[0] / rng.uniform(5, 12) * np.exp(np.linspace(0, rng.normal(0.1, 0.2), len(dates)))
        pe = close / eps
        if rng.random() < 0.1:  # 少数亏损股
            pe[-60:] = -abs(pe[-60:])
        pb = pe * rng.uniform(0.08, 0.15)
        return pd.DataFrame({"date": dates, "close": close.round(2), "pe": pe, "pb": pb, "mcap": close * rng.uniform(5e9, 5e10)})

    def valuation_since(self, codes: list[str], since: pd.Timestamp) -> dict[str, pd.DataFrame]:
        out = {}
        for code in codes:
            df = self.valuation(code)
            df = df[df["date"] > since].reset_index(drop=True)
            if not df.empty:
                out[code] = df
        return out

    def dividends(self, code: str) -> pd.DataFrame:
        rng = self._rng(code)
        rng.random(252 * 6 * 3)  # 与 valuation 错开随机序列
        val = DemoProvider.valuation(self, code)  # 不经过子类重载（测试中会统计请求次数）
        price0 = float(val["close"].iloc[0])
        base = price0 * rng.uniform(0.03, 0.07)
        growth = rng.normal(0.03, 0.06)
        cut_year = self.end.year - 1 if rng.random() < 0.1 else None
        rows = []
        for year in range(self.end.year - 8, self.end.year):
            dps = base * (1 + growth) ** (year - self.end.year + 8)
            if year == cut_year:
                dps *= 0.4
            eps = dps / rng.uniform(0.3, 0.8)
            interim = rng.random() < 0.4
            final = dps * (0.5 if interim else 1)
            if interim:
                rows.append((f"{year}-06-30", f"{year}-10-15", dps * 0.5, np.nan))
            rows.append((f"{year}-12-31", f"{year + 1}-07-10", final, eps))
        df = pd.DataFrame(rows, columns=["report_date", "ex_date", "dps", "eps"])
        df["report_date"] = pd.to_datetime(df["report_date"])
        df["ex_date"] = pd.to_datetime(df["ex_date"])
        df["dps"] = df["dps"].round(3)
        df["progress"] = "实施分配"
        future = df["ex_date"] > self.end
        df.loc[future, "progress"] = "股东大会决议通过"
        df.loc[future, "ex_date"] = pd.NaT
        return df
