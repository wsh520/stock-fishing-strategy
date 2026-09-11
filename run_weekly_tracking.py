"""周度追踪脚本：统计推荐个股的最新表现，落库 MySQL 并飞书汇总。

追踪口径：
- 每条推荐记录自推荐日起最多追踪一个月（TRACK_MAX_WEEKS=4 次周度记录，
  且推荐日起 TRACK_MAX_AGE_DAYS=31 天后强制退出，双保险）；
- 每次记录当时的最新收盘价：收益值 = 最新收盘价 - 推荐时收盘价，
  收益率 = 收益值 / 推荐时收盘价 × 100%；
- 同一股票在不同日期被重复推荐的，视为不同推荐记录，各自独立追踪。

由 GitHub Actions 每周五收盘后执行（weekly_tracking.yml），也可本地手动：
    python run_weekly_tracking.py
    python run_weekly_tracking.py --no-cache   # 跳过磁盘缓存，强制拉最新行情
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd

logger = logging.getLogger("weekly")


def _setup_logging() -> None:
    """统一日志格式：时间戳 + 级别 + 模块名，与 run.py 保持一致。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数解析。

    --no-cache：跳过磁盘缓存读写（cache/ 目录），强制从 Baostock/AkShare 拉最新行情。
    同一天与日任务同时执行、或怀疑缓存被污染时使用。内存 TTL 缓存仍生效。
    """
    parser = argparse.ArgumentParser(
        prog="run_weekly_tracking.py",
        description="周度追踪脚本：统计推荐个股最新表现并落库/通知",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用 cache/ 磁盘缓存读写，强制从数据源拉最新行情",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None):
    _setup_logging()
    args = _parse_args(argv)
    t_start = time.time()
    logger.info("=" * 60)
    logger.info("周度追踪任务启动")

    # 延迟导入策略模块（确保路径已设置）
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        CacheManager,
        get_daily_data,
        _bs_login,
        _bs_logout,
        _bs_state,
        _AK_AVAILABLE,
    )
    from store.mysql_store import (
        TRACK_MAX_WEEKS,
        get_active_recommendations,
        is_configured,
        save_tracking,
    )
    from notify.feishu import notify_tracking_result

    if not is_configured():
        logger.info("MySQL 未配置（MYSQL_HOST/USER/PASSWORD/DATABASE），周度追踪退出")
        return

    # Step 1: 查询仍在追踪期内的推荐记录（一个月内、未达 4 次追踪）
    logger.info("Step 1/4 查询追踪期内的推荐记录...")
    recs = get_active_recommendations()
    if not recs:
        logger.info("无追踪期内的推荐记录，任务结束")
        return
    logger.info("Step 1/4 完成：待追踪推荐记录 %d 条", len(recs))

    # Step 2: 登录数据源（Baostock 主 / AkShare 备，与选股主流程同一套连接管理）
    logger.info("Step 2/4 登录数据源（Baostock 主 / AkShare 备）...")
    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            logger.warning("Baostock 登录失败，本次降级 AkShare 拉取行情")
            _bs_state["circuit_open"] = True
        else:
            logger.error("Baostock 登录失败且未安装 AkShare，无法拉取行情，追踪终止")
            return
    logger.info("Step 2/4 完成：数据源就绪（Baostock %s）", "在线" if not _bs_state["circuit_open"] else "熔断，走 AkShare")

    config = StrategyConfig()
    if args.no_cache:
        config.USE_CACHE = False
        logger.info("已启用 --no-cache：跳过 cache/ 磁盘缓存读写，本次全部从数据源拉取")
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    def _fetch_close(rec: dict) -> tuple[dict, float | None, str | None]:
        """拉取个股最新收盘价及其对应的实际交易日。"""
        code = rec["code"]
        try:
            df = get_daily_data(code, config, cache)
            if df is None or df.empty:
                return rec, None, None
            last = df.iloc[-1]
            return rec, float(last["close"]), str(last["date"])
        except Exception as e:
            logger.debug("%s(%s) 拉取行情异常: %s", rec["name"], code, e)
            return rec, None, None

    # Step 3: 并发拉行情（bs_lock 保护 Baostock），主线程逐条落库
    logger.info("Step 3/4 并发拉取行情并逐条落库（%d 条，%d 线程）...", len(recs), config.MAX_WORKERS)
    report_rows: list[dict] = []
    tracked, failed = 0, 0
    try:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            futures = [pool.submit(_fetch_close, r) for r in recs]
            for fut in as_completed(futures):
                rec, close_price, close_date = fut.result()
                code, name = rec["code"], rec["name"]
                if close_price is None:
                    failed += 1
                    logger.warning("%s(%s) 行情获取失败，本次跳过", name, code)
                    continue

                # 行情未走出推荐日（如周任务与日任务同日执行）：收益恒为 0，
                # 跳过且不计入追踪次数，保证 4 次追踪都是有效观测
                rec_date_str = rec["rec_date"].strftime("%Y-%m-%d") if hasattr(rec["rec_date"], "strftime") else str(rec["rec_date"])
                if close_date <= rec_date_str:
                    logger.info("%s(%s) 收盘价仍为推荐日(%s)，本次不计追踪", name, code, rec_date_str)
                    continue

                week_no = int(rec["tracked_weeks"]) + 1
                ok = save_tracking(
                    rec_id=rec["id"],
                    rec_date=rec["rec_date"],
                    code=code,
                    week_no=week_no,
                    close_date=close_date,
                    close_price=close_price,
                    rec_close=float(rec["rec_close"]),
                )
                if ok:
                    tracked += 1
                    ret_pct = (close_price - float(rec["rec_close"])) / float(rec["rec_close"]) * 100
                    logger.info("%s(%s) 第%d周: %s -> %s (%+.2f%%)", name, code, rec["rec_close"], close_price, ret_pct)
                    report_rows.append({
                        "name": name,
                        "code": code,
                        "status": f"第{week_no}/{TRACK_MAX_WEEKS}周",
                        "rec_price": float(rec["rec_close"]),
                        "current_price": close_price,
                        "return_pct": round(ret_pct, 2),
                    })
    finally:
        _bs_logout()

    logger.info("Step 3/4 完成：成功 %d 条，失败 %d 条", tracked, failed)

    # Step 4: 飞书汇总通知（复用现有追踪报告卡片）
    if report_rows:
        logger.info("Step 4/4 发送飞书追踪汇总（%d 条）...", len(report_rows))
        notify_tracking_result(pd.DataFrame(report_rows))
    else:
        logger.info("Step 4/4 无有效追踪记录，跳过飞书通知")

    logger.info("任务全部完成，总耗时 %.1f 秒", time.time() - t_start)


if __name__ == "__main__":
    run()
