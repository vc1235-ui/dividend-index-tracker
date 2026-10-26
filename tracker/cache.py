"""估值历史的本地缓存，用于增量更新（在 GitHub Actions 中由 actions/cache 跨运行保存）。

每只股票一个 CSV（date, close, pe, pb, mcap），index.json 记录各股票最近一次全量拉取的日期。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

import pandas as pd

COLUMNS = ["date", "close", "pe", "pb", "mcap"]


def merge(old: pd.DataFrame, new: pd.DataFrame | None) -> pd.DataFrame:
    """合并缓存与新数据，同一日期以新数据为准。"""
    if new is None or new.empty:
        return old
    out = pd.concat([old, new[COLUMNS]], ignore_index=True)
    return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


class HistoryCache:
    def __init__(self, root: Path, keep_years: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.keep_years = keep_years
        self._index_path = self.root / "index.json"
        try:
            self._full_dates = json.loads(self._index_path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._full_dates = {}
        self._lock = threading.Lock()

    def load(self, code: str) -> tuple[pd.DataFrame | None, date | None]:
        """返回 (缓存的估值历史, 最近一次全量拉取日期)，没有缓存时为 (None, None)。"""
        path = self.root / f"{code}.csv"
        full = self._full_dates.get(code)
        if not path.exists() or not full:
            return None, None
        try:
            df = pd.read_csv(path, parse_dates=["date"])
        except Exception:
            return None, None
        if df.empty or list(df.columns) != COLUMNS:
            return None, None
        return df, date.fromisoformat(full)

    def save(self, code: str, df: pd.DataFrame, full: bool) -> None:
        """保存估值历史（只保留回看所需年数 + 1 年）。full=True 表示这是一次全量拉取。"""
        df = df[COLUMNS].dropna(subset=["date"]).sort_values("date")
        if not df.empty:
            df = df[df["date"] > df["date"].iloc[-1] - pd.DateOffset(years=self.keep_years + 1)]
        df.to_csv(self.root / f"{code}.csv", index=False)
        if full:
            with self._lock:
                self._full_dates[code] = date.today().isoformat()

    def flush(self) -> None:
        with self._lock:
            self._index_path.write_text(json.dumps(self._full_dates, ensure_ascii=False, indent=0), "utf-8")
