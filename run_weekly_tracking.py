"""周度追踪脚本：统计推荐个股的最新表现，落库 MySQL 并飞书汇总。

追踪口径：
- 用沪深300行情日期定位推荐后第5/10/15/20个市场交易日，60日内允许补跑；
- 仅记录目标日有效收盘价，停牌/缺价跳过，不以前后日期代替；
- 同一推荐同一收盘日及同一期限幂等，历史未知期限不猜测；
- **收益率 =（目标日收盘价 − 同序列推荐日收盘价）/ 同序列推荐日收盘价 × 100%**：
  close 取前复权（`ADJUST="qfq"`，以最新交易日为锚点），故分母必须用**同一次拉取**里
  推荐日的收盘价。用落库的「推荐日当时」价格当分母，会在追踪期内发生除权除息时把
  10 送 10 的含权收益（本应 0%）算成 −50%；
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


def select_observations(rec: dict, daily: pd.DataFrame, market: pd.DataFrame) -> list[dict]:
    """用市场交易日定位固定期限；停牌/缺价不前填，允许补跑多个到期观测。

    每条观测额外带 **base_close**：**同一次拉取**（即同一前复权序列）里推荐日的收盘价。
    调用方必须用它而不是落库的 `rec_close` 作收益率分母——见下方注释。
    """
    if daily is None or daily.empty or market is None or market.empty:
        return []
    from store.mysql_store import TRACK_HORIZONS
    rec_day = pd.Timestamp(rec["rec_date"]).normalize()
    calendar = pd.to_datetime(market["date"], errors="coerce").dropna().dt.normalize()
    calendar = calendar.drop_duplicates().sort_values()
    # 必须覆盖推荐日，避免截断历史使第5日错位；不使用未来日期。
    if rec_day not in set(calendar):
        return []
    calendar = calendar[(calendar > rec_day) & (calendar <= pd.Timestamp.today().normalize())]
    quotes = daily.copy()
    quotes["date"] = pd.to_datetime(quotes["date"], errors="coerce").dt.normalize()
    quotes["close"] = pd.to_numeric(quotes["close"], errors="coerce")
    quotes = quotes.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    quotes = quotes[(quotes["close"] > 0) & (quotes["close"] < float("inf"))]
    for column in ("volume", "amount"):
        if column in quotes:
            quotes = quotes[pd.to_numeric(quotes[column], errors="coerce") > 0]
    quotes = quotes.set_index("date")
    # 复权基准对齐（否则除权股会算出假暴跌）：close 一律取**前复权**（config.ADJUST="qfq"），
    # 而前复权以**最新交易日**为锚点——追踪期内一旦除权除息，历史价格会被整体重算。
    # 若拿落库的「推荐日当时的前复权价」当分母、拿本次拉取的收盘价当分子，两者基准不同：
    # 10 送 10 的含权收益本应为 0%，会被算成 −50%。故取同一次序列里推荐日的收盘价作基准。
    base_close = float(quotes.loc[rec_day, "close"]) if rec_day in quotes.index else None
    dates = {str(d).strip()[:10] for d in str(rec.get("tracked_close_dates") or "").split(",") if d}
    last_day = rec.get("last_close_date")
    if last_day is not None and not pd.isna(last_day):
        dates.add(pd.Timestamp(last_day).date().isoformat())
    horizons = {int(d) for d in str(rec.get("tracked_horizons") or "").split(",") if d}
    result = []
    for horizon in TRACK_HORIZONS:
        if horizon in horizons or len(calendar) < horizon:
            continue
        day = calendar.iloc[horizon - 1]
        day_str = day.date().isoformat()
        if day_str in dates or day not in quotes.index:
            continue
        result.append(dict(week_no=horizon // 5, holding_trade_days=horizon,
                           close_date=day_str, close_price=float(quotes.loc[day, "close"]),
                           base_close=base_close))
    return result


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
        get_index_daily,
        _bs_login,
        _bs_logout,
        _bs_state,
        _AK_AVAILABLE,
    )
    from store.mysql_store import (
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

    def _fetch_close(rec: dict) -> tuple[dict, list[dict] | None]:
        try:
            df = get_daily_data(rec["code"], config, cache)
            if df is None or df.empty:
                return rec, None
            return rec, select_observations(rec, df, market)
        except Exception as e:
            logger.debug("%s(%s) 拉取行情异常: %s", rec["name"], rec["code"], e)
            return rec, None

    # Step 3: 并发拉行情（bs_lock 保护 Baostock），主线程逐条落库
    logger.info("Step 3/4 并发拉取行情并逐条落库（%d 条，%d 线程）...", len(recs), config.MAX_WORKERS)
    report_rows: list[dict] = []
    tracked, failed = 0, 0
    try:
        market = get_index_daily(config, cache)
        if market is None or market.empty:
            logger.warning("市场交易日序列缺失，跳过固定期限追踪")
            return
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            futures = [pool.submit(_fetch_close, r) for r in recs]
            for fut in as_completed(futures):
                rec, observations = fut.result()
                code, name = rec["code"], rec["name"]
                if observations is None:
                    failed += 1
                    logger.warning("%s(%s) 行情获取失败，本次跳过", name, code)
                    continue

                for observation in observations:
                    # 收益率分母必须与分子同源（同一次前复权序列），见 select_observations
                    # 里 base_close 的说明：落库的 rec_close 是「推荐日当时」的前复权价，
                    # 追踪期内一旦除权除息，它与本次拉取的收盘价就不再同基准。
                    base_close = observation.pop("base_close", None)
                    if base_close is None:
                        base_close = float(rec["rec_close"])
                        logger.warning("%s(%s) 缺少同序列基准价（推荐日无行情），回退落库推荐价 %.3f",
                                       name, code, base_close)
                    elif abs(base_close - float(rec["rec_close"])) > 1e-6:
                        # 除权除息会让「同序列基准价」与落库值分叉，必须留痕——否则用户
                        # 看到卡片上的「推荐价」与当初通知不一致，会误以为数据出错。
                        logger.info("%s(%s) 复权基准已变（落库 %.3f → 同序列 %.3f），"
                                    "本期限收益率按同一前复权序列计算",
                                    name, code, float(rec["rec_close"]), base_close)
                    ok = save_tracking(rec_id=rec["id"], rec_date=rec["rec_date"], code=code,
                                       rec_close=base_close, **observation)
                    if ok:
                        tracked += 1
                        close_price = observation["close_price"]
                        horizon = int(observation["holding_trade_days"])
                        ret_pct = (close_price / base_close - 1) * 100
                        report_rows.append({
                            "name": name, "code": code,
                            "strategy": str(rec.get("strategy") or "bottom_fishing"),
                            # holding_trade_days 供通知层做「按固定期限分列」：混合口径把不同
                            # 成熟度的收益直接平均，只有同一期限的样本才可比
                            "holding_trade_days": horizon,
                            "status": f"推荐后{horizon}交易日",
                            # 展示价与收益率同为「前复权同序列」口径，用户可自行核算
                            "rec_price": base_close, "current_price": close_price,
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
