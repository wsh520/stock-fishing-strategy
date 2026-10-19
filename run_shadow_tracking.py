"""影子账本追踪：计算 pending / near-miss 候选的固定期限收益。

只读 `cache/shadow_recommendations.jsonl`（或 data/shadow/ 回退），写
`data/shadow/shadow_tracking.json`；不连 MySQL、不推送飞书、不改正式推荐。

为什么单独一个脚本：正式周跟踪只读 stock_recommendation 的 formal 行，pending
从未落库；本脚本复用同一套市场交易日/前复权/固定期限逻辑，把 near-miss 的
F5/F10/F15/F20/F40/F60 收益补成可评估的证据。它不构成买入建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from run_weekly_tracking import select_observations
from src.shadow_ledger import load_shadow_records, summarize_tracking

logger = logging.getLogger("shadow_track")
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_shadow_tracking.py",
                                     description="影子账本固定期限追踪（不通知、不落正式表）")
    parser.add_argument("--ledger", default=os.path.join(_PROJECT_ROOT, "cache",
                                                         "shadow_recommendations.jsonl"))
    parser.add_argument("--output", default=os.path.join(_PROJECT_ROOT, "data", "shadow",
                                                         "shadow_tracking.json"))
    parser.add_argument("--max-age-days", type=int, default=180,
                        help="只追踪最近 N 个日历日内的影子记录")
    parser.add_argument("--all", action="store_true",
                        help="追踪全部 pending；默认只追踪 is_near_miss（阻断≤2）")
    parser.add_argument("--no-cache", action="store_true", help="跳过 cache/ 磁盘缓存")
    return parser.parse_args(argv)


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _parse_args(argv)
    started = time.time()
    logger.info("=" * 60)
    logger.info("影子账本追踪启动（只读账本，不通知、不写正式推荐）")

    ledger_path = args.ledger
    if not os.path.exists(ledger_path):
        fallback = os.path.join(_PROJECT_ROOT, "data", "shadow", "shadow_recommendations.jsonl")
        if os.path.exists(fallback):
            ledger_path = fallback
    records_by_key = load_shadow_records(ledger_path)
    if not records_by_key:
        logger.info("影子账本为空或不存在（%s），任务结束", ledger_path)
        return 0
    cutoff = (datetime.now(timezone(timedelta(hours=8))).date()
              - timedelta(days=max(0, int(args.max_age_days)))).isoformat()
    records = [
        record for record in records_by_key.values()
        if str(record.get("rec_date") or "")[:10] >= cutoff
        and not record.get("superseded_by_formal")
        and (bool(args.all) or bool(record.get("is_near_miss")))
    ]
    logger.info("影子账本：累计 %d 条；最近 %d 天内、未被 formal 取代、%s %d 条（截止 %s）",
                len(records_by_key), int(args.max_age_days),
                "全部 pending" if args.all else "near-miss", len(records), cutoff)
    if not records:
        return 0

    from src.screening_engine import (
        CacheManager,
        StrategyConfig,
        _AK_AVAILABLE,
        _bs_login,
        _bs_logout,
        _bs_state,
        get_daily_data,
        get_index_daily,
    )

    config = StrategyConfig()
    if args.no_cache:
        config.USE_CACHE = False
        logger.info("已启用 --no-cache：本次影子追踪跳过 cache/")
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            logger.warning("Baostock 登录失败，影子追踪降级 AkShare")
            _bs_state["circuit_open"] = True
        else:
            logger.error("Baostock 登录失败且未安装 AkShare，影子追踪终止")
            return 0

    try:
        market = get_index_daily(config, cache)
        if market is None or market.empty:
            logger.warning("市场交易日序列缺失，影子追踪跳过")
            return 0

        # 按代码去重拉取：同一只票可能在不同日期多次进入 pending，周跟踪只应拉一次行情。
        codes = list(dict.fromkeys(str(record.get("code")) for record in records
                                   if str(record.get("code") or "").strip()))

        def fetch_code(code: str):
            try:
                daily = get_daily_data(code, config, cache)
                if daily is None or daily.empty:
                    return code, None
                return code, daily
            except Exception as exc:  # noqa: BLE001 单票失败不阻断整体
                logger.debug("%s 行情异常: %s", code, exc)
                return code, None

        daily_by_code: dict[str, pd.DataFrame] = {}
        fetched = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, int(config.MAX_WORKERS))) as pool:
            futures = [pool.submit(fetch_code, code) for code in codes]
            for future in as_completed(futures):
                code, daily = future.result()
                fetched += 1
                if daily is None:
                    failed += 1
                else:
                    daily_by_code[code] = daily
                if fetched % 20 == 0 or fetched == len(codes):
                    logger.info("影子追踪进度：%d/%d 只（失败 %d，可跟踪 %d）",
                                fetched, len(codes), failed, len(daily_by_code))

        observations: list[dict] = []
        records_with_quotes = 0
        for record in records:
            daily = daily_by_code.get(str(record.get("code")))
            if daily is None:
                continue
            records_with_quotes += 1
            rec = {
                "rec_date": record.get("rec_date"),
                "tracked_close_dates": "",
                "tracked_horizons": "",
            }
            for row in select_observations(rec, daily, market):
                base_close = row.get("base_close")
                close_price = row.get("close_price")
                if not base_close or not close_price:
                    continue
                observations.append(dict(
                    code=record.get("code"),
                    name=record.get("name"),
                    rec_date=record.get("rec_date"),
                    score=record.get("score"),
                    blockers=record.get("blockers") or [],
                    blocker_count=record.get("blocker_count"),
                    is_near_miss=bool(record.get("is_near_miss")),
                    operating_status=record.get("operating_status"),
                    holding_trade_days=row.get("holding_trade_days"),
                    close_date=row.get("close_date"),
                    close_price=round(float(close_price), 4),
                    base_close=round(float(base_close), 4),
                    return_pct=round((float(close_price) / float(base_close) - 1) * 100, 4),
                ))

        summary = summarize_tracking(observations)
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ledger_path": ledger_path,
            "ledger_records": len(records_by_key),
            "tracked_records": len(records),
            "records_with_quotes": records_with_quotes,
            "observations": len(observations),
            "max_age_days": int(args.max_age_days),
            "include_all": bool(args.all),
            "summary": summary,
            "rows": observations,
        }
        _atomic_write_json(args.output, payload)
        logger.info("影子追踪写入：%s（观测 %d 条）", args.output, len(observations))
        for row in summary.get("overall", []):
            logger.info("影子汇总 F%s：n=%d 均值=%s%% 中位=%s%% 胜率=%s",
                        row["holding_trade_days"], row["n"], row["mean"], row["median"],
                        row["win_rate"])
        logger.info("影子追踪完成，总耗时 %.1f 秒", time.time() - started)
        return 0
    finally:
        try:
            _bs_logout()
        except Exception:  # noqa: BLE001 清理失败不影响结果
            pass


if __name__ == "__main__":
    raise SystemExit(run())
