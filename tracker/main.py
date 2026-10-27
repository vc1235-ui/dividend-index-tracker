"""每日任务：拉取成分股数据 → 计算指标与信号 → 生成网页数据 → 对比状态发送提醒。

用法：
    python -m tracker.main                # 真实数据（AKShare）
    python -m tracker.main --demo         # 离线演示数据
    python -m tracker.main --no-notify    # 不发送提醒
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from . import backtest, indicators, notify, signals
from .cache import HistoryCache, merge
from .fetch import _retry

ROOT = Path(__file__).resolve().parent.parent
BACKFILL_VERSION = 1  # 用回测补充信号起始日期；状态中版本号不同时自动执行一次
log = logging.getLogger("tracker")
CST = timezone(timedelta(hours=8))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), "utf-8")


def collect_members(provider, cfg: dict, cache_path: Path) -> tuple[dict[str, dict], list[dict]]:
    """合并各指数成分股：code -> {name, indices: {指数代码: 权重}}。获取失败时使用上次缓存。

    每次运行都从中证指数官网重新获取，与上次的名单比较，返回成分股调整记录（调入 / 调出）。
    """
    cache = load_json(cache_path, {})
    members: dict[str, dict] = {}
    changes: list[dict] = []
    for idx in cfg["indices"]:
        code = idx["code"]
        try:
            df = provider.constituents(code)
            if df.empty:
                raise ValueError("成分股为空")
            rows = [
                {"code": str(r.code).zfill(6), "name": str(r.name), "weight": None if pd.isna(r.weight) else float(r.weight)}
                for r in df.itertuples()
            ]
            if code in cache:
                old = {r["code"]: r["name"] for r in cache[code]["rows"]}
                new = {r["code"]: r["name"] for r in rows}
                added = [{"code": c, "name": new[c]} for c in sorted(set(new) - set(old))]
                removed = [{"code": c, "name": old[c]} for c in sorted(set(old) - set(new))]
                if added or removed:
                    changes.append({"date": date.today().isoformat(), "index": code, "index_name": idx["name"],
                                    "added": added, "removed": removed})
                    log.info("%s 成分股调整：调入 %d 只，调出 %d 只", idx["name"], len(added), len(removed))
            cache[code] = {"updated": date.today().isoformat(), "rows": rows}
        except Exception as e:
            if code not in cache:
                raise
            log.error("指数 %s 成分股获取失败，使用 %s 的缓存: %s", code, cache[code]["updated"], e)
            rows = cache[code]["rows"]
        for r in rows:
            m = members.setdefault(r["code"], {"name": r["name"], "indices": {}})
            m["indices"][code] = r["weight"]
    dump_json(cache_path, cache)
    return members, changes


def diff_signals(stocks: list[dict], members: set[str], state: dict, data_date: str, ncfg: dict) -> list[dict]:
    """与上次状态比较，找出新进入 BUY/SELL 的股票，以及被调出指数的股票。"""
    prev = state.get("stocks", {})
    on = set(ncfg.get("on_signals", ["BUY", "SELL"]))
    cooldown = timedelta(days=ncfg.get("cooldown_days", 20))
    today = date.fromisoformat(data_date)
    alerts, new_state = [], {}

    def recently_alerted(p: dict, sig: str) -> bool:
        last = p.get("last_alert")
        return bool(last and last["signal"] == sig and today - date.fromisoformat(last["date"]) < cooldown)

    for s in stocks:
        p = prev.get(s["code"], {})
        entry = {
            "name": s["name"],
            "signal": s["signal"],
            "since": p.get("since", data_date) if p.get("signal") == s["signal"] else data_date,
            "since_capped": bool(p.get("since_capped")) and p.get("signal") == s["signal"],
            "last_alert": p.get("last_alert"),
        }
        if s["signal"] != p.get("signal") and s["signal"] in on and not recently_alerted(p, s["signal"]):
            alerts.append({k: s.get(k) for k in ("code", "name", "signal", "reasons", "price", "yield", "score")})
            entry["last_alert"] = {"signal": s["signal"], "date": data_date}
        new_state[s["code"]] = entry

    for code, p in prev.items():
        if code in new_state:
            continue
        if code in members:  # 本次处理失败的股票，保留原状态
            new_state[code] = p
        elif p.get("removed") is None:
            if "SELL" in on and p.get("signal") != "SELL":
                alerts.append({"code": code, "name": p["name"], "signal": "SELL", "reasons": ["已被调出跟踪的红利指数"]})
            new_state[code] = {**p, "removed": data_date}
    state["stocks"] = new_state
    return alerts


def incremental_valuations(provider, codes: list[str], cache: HistoryCache, cfg: dict) -> dict[str, pd.DataFrame]:
    """对有缓存的股票，用一次批量请求补上缓存之后的新数据。

    每次运行只对少量股票轮流全量刷新：缓存超过 full_refresh_days 天未全量刷新的股票中，
    取最久未刷新的 full_refresh_per_run 只不走增量（返回值中不包含），其余照常增量；
    没有缓存或缓存最新日期太旧的股票也不走增量。
    用其中一只股票的全量数据核对增量结果，不一致则整体放弃增量（调用方会改为全量拉取）。
    """
    if not hasattr(provider, "valuation_since"):
        return {}
    today = date.today()
    full_days = int(cfg.get("full_refresh_days", 7))
    per_run = int(cfg.get("full_refresh_per_run", 12))
    cached, stale = {}, []
    for code in codes:
        df, full_date = cache.load(code)
        if df is None or (today - df["date"].max().date()).days >= 30:
            continue
        cached[code] = df
        if (today - full_date).days >= full_days:
            stale.append((full_date, code))
    for _, code in sorted(stale)[:per_run]:
        del cached[code]
    if stale:
        log.info("轮换全量刷新 %d 只（到期 %d 只）", min(per_run, len(stale)), len(stale))
    if not cached:
        return {}
    since = min(df["date"].max() for df in cached.values())
    try:
        new = _retry(provider.valuation_since, sorted(cached), since)
        merged = {code: merge(df, new.get(code)) for code, df in cached.items()}
        probe = sorted(cached)[0]
        full = provider.valuation(probe)
        if full["date"].max() > merged[probe]["date"].max():
            raise ValueError(f"{probe} 增量数据缺少 {full['date'].max():%Y-%m-%d}")
    except Exception as e:
        log.warning("增量更新失败，改为全量拉取: %s", e)
        return {}
    log.info("增量更新 %d 只股票（自 %s 起，%d 行新数据）", len(merged), f"{since:%Y-%m-%d}", sum(len(v) for v in new.values()))
    return merged


def load_industries(provider, codes: list[str], path: Path, max_age_days: int = 90) -> dict[str, str]:
    """所属行业：缓存在 state/industries.json 中，只为缺失或超过 max_age_days 天的股票批量请求一次。"""
    cache = load_json(path, {})
    today = date.today()
    todo = [c for c in codes
            if c not in cache or (today - date.fromisoformat(cache[c]["updated"])).days >= max_age_days]
    if todo and hasattr(provider, "industries"):
        try:  # 行业只是辅助信息：快速失败，不拖慢主流程，下次运行再试
            got = _retry(provider.industries, todo, tries=2, wait=2)
            for code, name in got.items():
                if code in todo:
                    cache[code] = {"industry": name, "updated": today.isoformat()}
            log.info("行业数据：请求 %d 只，取得 %d 只，缓存共 %d 只", len(todo), len(got), len(cache))
            dump_json(path, cache)
        except Exception as e:
            log.warning("行业数据获取失败，本次显示为“未知”: %s", e)
    # 申万二级行业与一级同名时带“Ⅱ”后缀（如“银行Ⅱ”），显示时去掉
    return {c: cache[c]["industry"].rstrip("Ⅱ").strip() if c in cache else "未知" for c in codes}


def backfill_since(stocks: list[dict], history: dict[str, pd.DataFrame], state: dict, max_buys: int) -> None:
    """用回测结果补充信号起始日期（只提前、不推后），写入 state 并标记已执行。"""
    backtest.apply_daily_cap(history, max_buys)
    streaks = backtest.signal_streaks(history, {s["code"]: s["signal"] for s in stocks})
    entries = state.setdefault("stocks", {})
    changed = 0
    for s in stocks:
        entry = entries.get(s["code"])
        if s["code"] not in streaks or not entry or entry.get("signal") != s["signal"]:
            continue
        since, capped = streaks[s["code"]]
        if not entry.get("since") or since < entry["since"]:
            entry["since"], entry["since_capped"] = since, capped
            changed += 1
    state["backfill_version"] = BACKFILL_VERSION
    log.info("回测补充信号起始日期：%d 只（回测 %d 只、最近 %d 个交易日）", changed, len(history), backtest.HORIZON)


def run(provider, cfg: dict, web_dir: Path, state_dir: Path, send: bool, demo: bool = False,
        cache: HistoryCache | None = None) -> dict:
    rules = cfg["rules"]
    data_dir = web_dir / "data"
    if (data_dir / "stocks").exists():
        shutil.rmtree(data_dir / "stocks")

    members, member_changes = collect_members(provider, cfg, state_dir / "constituents.json")
    need_backfill = demo or load_json(state_dir / "state.json", {}).get("backfill_version") != BACKFILL_VERSION
    index_names = {i["code"]: i["name"] for i in cfg["indices"]}
    log.info("共 %d 只成分股（去重后）", len(members))

    workers = 1 if demo else max(1, int(cfg.get("workers", 4)))
    industries = load_industries(provider, sorted(members), state_dir / "industries.json")
    incremental = incremental_valuations(provider, sorted(members), cache, cfg) if cache else {}

    def fetch(code: str) -> tuple[dict, dict, pd.DataFrame | None]:
        if code in incremental:
            val = incremental[code]
        else:
            val = provider.valuation(code)
        if cache:
            cache.save(code, val, full=code not in incremental)
        div = provider.dividends(code)
        if not demo:
            time.sleep(cfg.get("request_interval", 0.5))
        grid = cfg.get("grid", {})
        m, series = indicators.compute(val, div, cfg["history_years"], rules["sell"]["dividend_cut_ratio"], grid)
        hist = (backtest.daily_signals(val, div, rules, cfg["history_years"], rules["sell"]["dividend_cut_ratio"])
                if need_backfill else None)
        return m, series, hist

    # 耗时几乎全在网络请求上，多线程并发拉取；并发失败的再逐只串行补拉一次
    results: dict[str, tuple[dict, dict, pd.DataFrame | None]] = {}
    errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, code): code for code in sorted(members)}
        for n, fut in enumerate(as_completed(futures), 1):
            code = futures[fut]
            try:
                results[code] = fut.result()
                log.info("[%d/%d] %s %s 完成", n, len(members), code, members[code]["name"])
            except Exception as e:
                errors[code] = e
                log.warning("[%d/%d] %s %s 失败，稍后重试: %s", n, len(members), code, members[code]["name"], e)
    for code in sorted(errors):
        try:
            results[code] = fetch(code)
            log.info("%s %s 重试成功", code, members[code]["name"])
        except Exception as e:
            log.error("%s %s 处理失败: %s", code, members[code]["name"], e)

    if cache:
        cache.flush()

    stocks, failed = [], []
    for code, info in sorted(members.items()):
        if code not in results:
            failed.append(code)
            continue
        m, series, _ = results[code]
        sc = signals.score(m)
        m["score"] = sc["total"]
        sig, reasons, quality = signals.evaluate(m, rules)
        stock = {"code": code, "name": info["name"], "industry": industries[code], "indices": info["indices"], **m,
                 "score_parts": sc["parts"], "quality": quality, "signal": sig, "reasons": reasons}
        stocks.append(stock)
        dump_json(data_dir / "stocks" / f"{code}.json", {**series, "code": code, "name": info["name"]})
        log.info("%s %s 股息率=%s 评分=%s 规则信号=%s", code, info["name"], m["yield"], m["score"], sig)

    signals.limit_buys(stocks, int(rules["buy"].get("max_daily", 5)))
    picks = sorted((s for s in stocks if s["signal"] == signals.BUY), key=lambda s: s["buy_rank"])
    log.info("今日买入推荐 %d 只：%s；买入候选 %d 只", len(picks), "、".join(s["name"] for s in picks) or "无",
             sum(bool(s.get("buy_candidate")) for s in stocks))

    if not stocks:
        raise RuntimeError("全部股票处理失败，请检查数据源")
    if len(failed) > len(members) * 0.3:
        raise RuntimeError(f"{len(failed)}/{len(members)} 只股票处理失败，数据源可能异常，本次不更新")

    data_date = max(s["date"] for s in stocks)
    state_path, log_path = state_dir / "state.json", state_dir / "signals_log.json"
    changes_path = state_dir / "constituent_changes.json"
    state = load_json(state_path, {})
    history = load_json(log_path, [])
    changes_log = load_json(changes_path, [])
    if demo:
        state, history, changes_log = {}, [], []
    if member_changes:
        changes_log = (member_changes + changes_log)[:50]
        if not demo:
            dump_json(changes_path, changes_log)

    alerts: list[dict] = []
    if state.get("last_data_date") == data_date:
        log.info("数据日期 %s 已处理过（非交易日或重复运行），不再生成提醒", data_date)
    else:
        alerts = diff_signals(stocks, set(members), state, data_date, cfg["notify"])
        state["last_data_date"] = data_date
        for a in alerts:
            history.insert(0, {"date": data_date, **a})
        history = history[:500]
        if not demo:
            dump_json(state_path, state)
            dump_json(log_path, history)

    if need_backfill:
        backfill_since(stocks, {c: r[2] for c, r in results.items() if r[2] is not None}, state,
                       int(rules["buy"].get("max_daily", 5)))
        if not demo:
            dump_json(state_path, state)

    prev = state.get("stocks", {})
    for s in stocks:
        # 状态里的起始日期只在信号一致时有效（同一天重复运行时状态不更新，信号可能已因规则调整而变化）
        p = prev.get(s["code"], {})
        same = p.get("signal") == s["signal"]
        s["since"] = p.get("since") if same else data_date
        s["since_capped"] = bool(p.get("since_capped")) and same  # 信号在整个回测区间内未变，实际天数更长
        # 信号持续的交易日数（含当天），按该股自身的交易日计算
        s["signal_days"] = sum(d >= s["since"] for d in results[s["code"]][1]["dates"]) if s["since"] else None

    summary = {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "data_date": data_date,
        "demo": demo,
        "indices": {c: {"name": index_names[c], "count": sum(c in s["indices"] for s in stocks)} for c in index_names},
        "rules": rules,
        "history_years": cfg["history_years"],
        "failed": failed,
        "constituent_changes": changes_log[:20],
        "stocks": sorted(stocks, key=lambda s: -s["score"]),
    }
    dump_json(data_dir / "summary.json", summary)
    dump_json(data_dir / "signals_log.json", history)

    # 只在出现新的买入 / 卖出信号或成分股调整时通知；信号持续不再通知
    if send:
        notify.dispatch(alerts, data_date, cfg["notify"], os.getenv("SITE_URL"), member_changes)
    log.info("完成：%d 只股票，%d 条新提醒，%d 项成分股调整，%d 只失败",
             len(stocks), len(alerts), len(member_changes), len(failed))
    return {"stocks": stocks, "alerts": alerts, "data_date": data_date, "member_changes": member_changes}


def main() -> None:
    ap = argparse.ArgumentParser(description="上证红利 / 中证红利 成分股跟踪")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--web", default=str(ROOT / "web"))
    ap.add_argument("--state", default=str(ROOT / "state"))
    ap.add_argument("--demo", action="store_true", help="使用离线演示数据")
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "history"), help="估值历史缓存目录（增量更新用）")
    ap.add_argument("--no-notify", action="store_true", help="不发送提醒")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text("utf-8"))
    if args.demo:
        from .fetch import DemoProvider

        provider = DemoProvider()
    else:
        from .fetch import AkshareProvider

        provider = AkshareProvider()
    state_dir = Path(args.state)
    if args.demo:  # 演示模式不污染真实状态
        state_dir = Path(args.web) / ".demo-state"
    cache = None if args.demo else HistoryCache(Path(args.cache), cfg["history_years"])
    run(provider, cfg, Path(args.web), state_dir, send=not args.no_notify and not args.demo, demo=args.demo, cache=cache)


if __name__ == "__main__":
    main()
