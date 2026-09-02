"""
每日数据增量同步模块

功能：
1. 同步全A股票列表到 DB（stock_info 表）
2. 增量同步个股日线（kline_daily 表）— 只拉取 DB 中最新日期之后的数据
3. 增量同步个股周线（kline_weekly 表）— 同上
4. 同步指数日线（index_daily 表）— 沪深300等
5. 所有写入使用 UPSERT，保证幂等可重跑

用法：
    python src/sync_data.py                    # 增量同步（默认）
    python src/sync_data.py --full             # 全量同步（首次初始化）
    python src/sync_data.py --full --start 20240101  # 指定起始日期全量
    python src/sync_data.py --workers 8        # 调整并发数
    python src/sync_data.py --codes 000001,600519  # 仅同步指定股票
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# 确保路径正确
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from src.bottom_fishing_strategy import (
    StrategyConfig,
    CacheManager,
    _fetch_stock_list_raw,
    _apply_universe_filters,
    _fetch_with_retry,
    _normalize_hist,
    _fetch_index_daily_raw,
    _ak_code_to_symbol,
)
from db import get_mysql_store

import akshare as ak


# ===========================================================================
# 同步配置
# ===========================================================================

DEFAULT_FULL_START = "20240101"  # 全量同步默认起始日期
SYNC_BATCH_SIZE = 100            # 每批打印进度的股票数


# ===========================================================================
# 核心同步函数
# ===========================================================================


def sync_stock_list(db, config: StrategyConfig) -> int:
    """同步全A股票列表到 DB。

    拉取原始列表 → 应用过滤 → 写入 stock_info 表。
    返回写入行数。
    """
    print("[SYNC] 同步股票列表...")
    log_id = db.log_sync_start("stock_list", datetime.now().strftime("%Y-%m-%d"))

    try:
        raw_df = _fetch_stock_list_raw(config)
        if raw_df is None or raw_df.empty:
            db.log_sync_finish(log_id, 0, "failed", "获取股票列表返回空")
            print("[SYNC] ✗ 获取股票列表失败")
            return 0

        filtered_df = _apply_universe_filters(raw_df, config)
        count = db.save_stock_info(filtered_df)
        db.log_sync_finish(log_id, count, "success")
        print(f"[SYNC] ✓ 股票列表: {count} 只")
        return count
    except Exception as e:
        db.log_sync_finish(log_id, 0, "failed", str(e)[:500])
        print(f"[SYNC] ✗ 股票列表异常: {e}")
        return 0


def _sync_one_stock_daily(
    code: str, db, config: StrategyConfig, start_date: str, end_date: str,
) -> int:
    """同步单只股票的日线数据。返回写入行数。"""
    symbol = _ak_code_to_symbol(code)

    # 查询 DB 中该股票的最新日期，实现增量
    latest = db.get_latest_kline_date("kline_daily", code=code)
    if latest and latest >= end_date:
        return 0  # 已是最新

    fetch_start = latest if latest else start_date
    # 如果从 DB 续接，从最新日期的下一天开始拉
    if latest:
        next_day = (datetime.strptime(latest, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        fetch_start = next_day

    if fetch_start > end_date:
        return 0

    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=fetch_start, end_date=end_date,
            adjust=config.ADJUST,
        ),
        config.MAX_RETRY,
        f"sync_daily({symbol})",
    )
    df = _normalize_hist(raw)
    if df is None or df.empty:
        return 0

    return db.save_kline_daily(code, df)


def _sync_one_stock_weekly(
    code: str, db, config: StrategyConfig, start_date: str, end_date: str,
) -> int:
    """增量同步单只股票的周线数据。返回写入行数。

    周线从 AkShare API 独立拉取（不从日线聚合，避免日线缺失导致周线失真）。
    通过查询 DB 中最新周线日期实现增量：只拉取上次之后的新数据。
    设计为每周六执行一次，工作日跳过。
    """
    symbol = _ak_code_to_symbol(code)

    latest = db.get_latest_kline_date("kline_weekly", code=code)
    if latest and latest >= end_date:
        return 0  # 已是最新

    fetch_start = latest if latest else start_date
    if latest:
        next_day = (datetime.strptime(latest, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        fetch_start = next_day

    if fetch_start > end_date:
        return 0

    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="weekly",
            start_date=fetch_start, end_date=end_date,
            adjust=config.ADJUST,
        ),
        config.MAX_RETRY,
        f"sync_weekly({symbol})",
    )
    df = _normalize_hist(raw)
    if df is None or df.empty:
        return 0

    return db.save_kline_weekly(code, df)


def sync_klines(
    db,
    config: StrategyConfig,
    stocks: list[dict],
    start_date: str,
    end_date: str,
    max_workers: int = 4,
    sync_period: str = "both",  # "daily" / "weekly" / "both"
) -> dict:
    """批量同步个股 K 线数据。

    Args:
        sync_period: "daily" 仅日线, "weekly" 仅周线, "both" 两者都同步

    Returns:
        {"daily_rows": int, "weekly_rows": int, "daily_stocks": int, "weekly_stocks": int}
    """
    today = datetime.now().strftime("%Y-%m-%d")
    total = len(stocks)
    result = {"daily_rows": 0, "weekly_rows": 0, "daily_stocks": 0, "weekly_stocks": 0}

    # --- 日线同步 ---
    if sync_period in ("daily", "both"):
        log_id = db.log_sync_start("daily", today)
        print(f"[SYNC] 同步日线: {total} 只股票, {start_date} ~ {end_date}, workers={max_workers}")
        completed = 0
        daily_rows = 0
        daily_stocks = 0
        errors = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _sync_one_stock_daily, s["code"], db, config, start_date, end_date,
                ): s["code"]
                for s in stocks
            }
            for f in as_completed(futures):
                completed += 1
                code = futures[f]
                try:
                    rows = f.result()
                    if rows > 0:
                        daily_rows += rows
                        daily_stocks += 1
                except Exception as e:
                    errors += 1
                    if errors <= 10:
                        print(f"[SYNC]   ✗ {code}: {e}")

                if completed % SYNC_BATCH_SIZE == 0 or completed == total:
                    print(f"[SYNC]   日线进度: {completed}/{total}, "
                          f"有效写入: {daily_rows} 行 ({daily_stocks} 只)")

        status = "success" if errors == 0 else f"partial({errors} errors)"
        db.log_sync_finish(log_id, daily_rows, status)
        result["daily_rows"] = daily_rows
        result["daily_stocks"] = daily_stocks
        print(f"[SYNC] ✓ 日线完成: {daily_rows} 行, {daily_stocks} 只股票有更新")

    # --- 周线同步（从 AkShare API 增量拉取，每周六执行一次） ---
    if sync_period in ("weekly", "both"):
        log_id = db.log_sync_start("weekly", today)
        print(f"[SYNC] 同步周线: {total} 只股票, {start_date} ~ {end_date}, workers={max_workers}")
        completed = 0
        weekly_rows = 0
        weekly_stocks = 0
        errors = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _sync_one_stock_weekly, s["code"], db, config, start_date, end_date,
                ): s["code"]
                for s in stocks
            }
            for f in as_completed(futures):
                completed += 1
                code = futures[f]
                try:
                    rows = f.result()
                    if rows > 0:
                        weekly_rows += rows
                        weekly_stocks += 1
                except Exception as e:
                    errors += 1
                    if errors <= 10:
                        print(f"[SYNC]   ✗ {code}: {e}")

                if completed % SYNC_BATCH_SIZE == 0 or completed == total:
                    print(f"[SYNC]   周线进度: {completed}/{total}, "
                          f"有效写入: {weekly_rows} 行 ({weekly_stocks} 只)")

        status = "success" if errors == 0 else f"partial({errors} errors)"
        db.log_sync_finish(log_id, weekly_rows, status)
        result["weekly_rows"] = weekly_rows
        result["weekly_stocks"] = weekly_stocks
        print(f"[SYNC] ✓ 周线完成: {weekly_rows} 行, {weekly_stocks} 只股票有更新")

    return result


def sync_index_daily(
    db,
    config: StrategyConfig,
    symbol: str = "sh000300",
    start_date: str = "",
    end_date: str = "",
) -> int:
    """同步指数日线数据到 DB。

    指数日线接口不支持区间参数，全量拉取后 UPSERT。
    返回写入行数。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_id = db.log_sync_start("index", today)

    try:
        print(f"[SYNC] 同步指数日线: {symbol}")
        df = _fetch_index_daily_raw(symbol, config)
        if df is None or df.empty:
            db.log_sync_finish(log_id, 0, "failed", "获取指数数据返回空")
            print(f"[SYNC] ✗ 指数 {symbol} 获取失败")
            return 0

        # 可选：按日期范围过滤
        if start_date or end_date:
            dt = pd.to_datetime(df["date"])
            mask = pd.Series(True, index=df.index)
            if start_date:
                s = start_date.replace("-", "")
                s_fmt = f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else start_date
                mask &= dt >= pd.to_datetime(s_fmt)
            if end_date:
                e = end_date.replace("-", "")
                e_fmt = f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else end_date
                mask &= dt <= pd.to_datetime(e_fmt)
            df = df[mask].copy()

        count = db.save_index_daily(symbol, df)
        db.log_sync_finish(log_id, count, "success")
        print(f"[SYNC] ✓ 指数 {symbol}: {count} 行")
        return count
    except Exception as e:
        db.log_sync_finish(log_id, 0, "failed", str(e)[:500])
        print(f"[SYNC] ✗ 指数异常: {e}")
        return 0


# ===========================================================================
# 主入口
# ===========================================================================


def run_sync(
    full: bool = False,
    start_date: str = "",
    end_date: str = "",
    max_workers: int = 4,
    codes: Optional[list[str]] = None,
    sync_period: str = "both",
) -> dict:
    """执行完整的数据同步流程。

    Args:
        full: True=全量同步, False=增量同步
        start_date: 全量模式起始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD), 默认今天
        max_workers: 并发线程数
        codes: 仅同步指定股票代码列表
        sync_period: "daily" / "weekly" / "both"

    Returns:
        同步结果摘要 dict
    """
    db = get_mysql_store()
    if db is None:
        print("[ERROR] MySQL 未配置或连接失败，无法执行数据同步")
        print("[HINT] 请设置环境变量: MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE")
        return {"error": "MySQL unavailable"}

    config = StrategyConfig()
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    end_date = end_date or datetime.now().strftime("%Y%m%d")
    if full:
        start_date = start_date or DEFAULT_FULL_START
    else:
        # 增量模式：从近3个月开始（覆盖指标计算所需的回看期）
        start_date = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")

    print("=" * 60)
    print(f"  数据同步 {'(全量)' if full else '(增量)'}")
    print(f"  日期范围: {start_date} ~ {end_date}")
    print(f"  并发数: {max_workers}")
    print(f"  同步内容: {sync_period}")
    print("=" * 60)

    t0 = time.time()

    # Step 1: 同步股票列表
    list_count = sync_stock_list(db, config)

    # Step 2: 获取待同步的股票列表
    if codes:
        stocks = [{"code": c.zfill(6), "name": ""} for c in codes]
        print(f"[SYNC] 指定同步 {len(stocks)} 只股票")
    else:
        # 优先从 DB 读取，降级到 API
        stock_df = db.get_stock_list_from_db()
        if stock_df is not None and not stock_df.empty:
            stocks = stock_df.to_dict("records")
        else:
            from src.bottom_fishing_strategy import get_stock_list
            stocks = get_stock_list(config, cache)

        if not stocks:
            print("[ERROR] 无法获取股票列表，同步终止")
            return {"error": "no stocks"}
        print(f"[SYNC] 待同步股票: {len(stocks)} 只")

    # Step 3: 同步 K 线
    kline_result = sync_klines(
        db, config, stocks, start_date, end_date,
        max_workers=max_workers, sync_period=sync_period,
    )

    # Step 4: 同步指数
    index_rows = sync_index_daily(db, config, config.CSI300_AK_SYMBOL, start_date, end_date)

    elapsed = time.time() - t0
    summary = {
        "mode": "full" if full else "incremental",
        "start_date": start_date,
        "end_date": end_date,
        "stock_list_count": list_count,
        "daily_rows": kline_result["daily_rows"],
        "weekly_rows": kline_result["weekly_rows"],
        "index_rows": index_rows,
        "elapsed_seconds": round(elapsed, 1),
    }

    print("\n" + "=" * 60)
    print("  同步完成")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  股票列表: {list_count} 只")
    print(f"  日线写入: {kline_result['daily_rows']} 行")
    print(f"  周线写入: {kline_result['weekly_rows']} 行")
    print(f"  指数写入: {index_rows} 行")
    print("=" * 60)

    return summary


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A股数据增量同步工具")
    parser.add_argument("--full", action="store_true", help="全量同步（默认增量）")
    parser.add_argument("--start", default="", help="全量模式起始日期 YYYYMMDD")
    parser.add_argument("--end", default="", help="结束日期 YYYYMMDD（默认今天）")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数（默认4）")
    parser.add_argument("--codes", default="", help="仅同步指定股票，逗号分隔")
    parser.add_argument("--period", choices=["daily", "weekly", "both"], default="both",
                        help="同步周期: daily/weekly/both（默认both）")
    args = parser.parse_args()

    code_list = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else None

    result = run_sync(
        full=args.full,
        start_date=args.start,
        end_date=args.end,
        max_workers=args.workers,
        codes=code_list,
        sync_period=args.period,
    )

    sys.exit(0 if "error" not in result else 1)
