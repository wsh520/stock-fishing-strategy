#!/usr/bin/env python3
"""
长期优质低估低位选股策略 - 执行入口

用法：
    python run_longterm.py                    # 正常运行
    python run_longterm.py --no-cache         # 跳过缓存
    python run_longterm.py --no-save          # 不落库（测试用）
    python run_longterm.py --no-notify        # 不推送飞书
    python run_longterm.py --compare          # 对比生产 quality_value 口径差异

与 run.py 的关系（2026-10-13 重写）：
    本脚本此前是一份**独立复刻**的编排层，与 run.py / src 层的真实接口全面脱节：
      · 导入不存在的 notify.feishu.send_stock_recommendations → 导入期即 ImportError；
      · 以 config=/cache=/stocks=/evaluator=... 调用 run_concurrent_screen，
        而真实签名是 (stock_list, screen_one, config, log) 且返回三元组；
      · 以 _rank_signals(df, config) 调用，实际只接受一个参数；
      · 手工重写了取数/并发/排序/行业分散/落库/通知全链路，
        因此漏掉了 market_crash_halt、pending/volatile 分层、估值口径声明、
        tier 过滤、闸门归因日志等一整套生产已验证的逻辑。
    现在改为与 run.py 同构的四步流水线，只通过「配置类 + 评估器」两个参数注入
    长期口径，其余（并发骨架、漏斗日志、分层、行业去重、落库、通知）全部复用
    src 层同一份实现——避免第二套编排再次与生产漂移。

长期口径与生产 quality_value 的差异（全部为**收紧**，只做减法）：
    1. 连续 5 年质量（vs 3 年），中位 ROE≥12%、最低 ROE≥8%
    2. 新增 ROE 稳定性（变异系数 ≤ 0.5）
    3. 新增盈利持续性（近 5 年年报中至少 3 次净利同比为正）
    4. 新增 5 年累计经营现金流/净利润 ≥ 1.0
    5. 新增长期投资资质综合评分 ≥ 60
    6. 新增相对强度硬门槛（近 60 日跑输沪深300 不超过 15 个百分点）
    7. 新增下降通道加速下跌否决（MA20 近 5 日跌幅 > 5%）
    8. 行业相对估值分位收紧至 ≤50%（vs 60%）；流动性 5000 万（vs 3000 万）
    9. 推荐数量 牛3/中2/熊1（vs 5/4/2）；持有期 30~90 天（vs 10~20 天）

止跌确认口径说明：保持基类 QV_STABILIZATION_MODE="strict"（与生产同源）。
    早期版本设为 "risk_only"，但 risk_only 把「站上 MA20」从否决降级为标签，
    等于放行更多票，与项目「宁缺毋滥」的最高优先级原则冲突，已撤回。
"""

import argparse
import logging
import os
import sys
import time
import traceback

import pandas as pd

# 将项目根目录加入 Python 路径（确保 src / store / notify 包可导入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notify.feishu import notify_screening_result
from store.mysql_store import STRATEGY_LONGTERM_QUALITY, save_recommendations

logger = logging.getLogger("longterm")


def _setup_logging() -> None:
    """统一日志格式：时间戳 + 级别 + 模块名，与 run.py / run_breakout.py 一致。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _valuation_mode_of(df) -> str | None:
    """从策略层写入的 valuation_mode 列推断本次实际生效的估值口径。

    与 run.py 同实现：口径悄悄从「行业相对」回退到「绝对阈值」时必须显式声明，
    否则用户会把两份不同口径的名单直接对比。返回 None 表示无需声明。
    """
    if df is None or getattr(df, "empty", True) or "valuation_mode" not in df.columns:
        return None
    modes = {str(m) for m in df["valuation_mode"].dropna().tolist()}
    if modes == {"industry"}:
        return "industry"
    if modes == {"absolute"}:
        return "absolute"
    if modes:
        return "mixed"
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数解析（与 run.py 同名开关保持一致语义）。

    --no-save / --no-notify：纯测试运行。实盘推荐表以 (rec_date, code, strategy)
    为唯一键且跳过 30 天内重复行，白天测试写入的推荐会与晚间正式结果并存于同一天，
    污染周度追踪与归因。纯测试请务必带上这两个开关。
    """
    parser = argparse.ArgumentParser(
        prog="run_longterm.py",
        description="长期优质低估低位选股策略入口脚本",
    )
    parser.add_argument("--no-cache", action="store_true",
                        help="禁用 cache/ 磁盘缓存读写，强制从数据源拉最新数据")
    parser.add_argument("--no-save", action="store_true",
                        help="跳过 MySQL 落库（测试用，避免污染实盘推荐与追踪数据）")
    parser.add_argument("--no-notify", action="store_true",
                        help="跳过飞书通知（测试用）")
    parser.add_argument("--compare", action="store_true",
                        help="对比模式：同时运行生产 quality_value 口径并打印差异")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    """主执行流程（与 run.py 同构的四步流水线）"""
    _setup_logging()
    args = _parse_args(argv)

    from src.bottom_fishing_strategy import is_data_degraded
    from src.quality_longterm_strategy import (
        LongTermQualityConfig,
        evaluate_quality_longterm,
    )
    # 复用基类的编排与并发骨架，只替换「配置类」与「评估器」两个口径入口。
    # 这样市场熔断、regime 数量收缩、闸门归因日志、pending/volatile 分层、
    # 行业去重、估值口径标注全部与生产共用同一份实现，不会二次漂移。
    from src import bottom_fishing_strategy as bfs

    t_start = time.time()
    logger.info("=" * 60)
    logger.info("长期优质低估低位选股任务启动")
    if args.no_save:
        logger.info("已启用 --no-save：本次不写 MySQL（纯测试运行）")
    if args.no_notify:
        logger.info("已启用 --no-notify：本次不推送飞书")

    config = LongTermQualityConfig()
    if args.no_cache:
        config.USE_CACHE = False
        logger.info("已启用 --no-cache：跳过 cache/ 磁盘缓存读写")
    cache = bfs.CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    market_env_desc = "unknown"

    # 长期口径的关键闸门值打进日志：口径调整后无需翻代码即可核对当次生效值。
    logger.info(
        "生效口径：年度质量=%d年(中位ROE≥%.0f%%/最低≥%.0f%%) | ROE稳定性CV≤%.2f | "
        "现金转换≥%.2f(5年累计) | 资质评分≥%.0f | RS≥%.0fpp | 行业PE分位≤%.0f%% | "
        "止跌模式=%s | 质量分≥%.0f | 数量 牛%d/中%d/熊%d | 持有 %d~%d 天",
        config.QUALITY_YEARS, config.QUALITY_MEDIAN_ROE_MIN, config.QUALITY_MIN_ROE,
        config.MAX_ROE_CV, config.LONGTERM_CASH_QUALITY_MIN, config.MIN_LONGTERM_QUALITY_SCORE,
        config.RS_MIN_THRESHOLD, config.VALUATION_INDUSTRY_PERCENTILE_MAX * 100,
        config.QV_STABILIZATION_MODE, config.MIN_QUALITY_SCORE,
        config.MAX_PICKS, config.NEUTRAL_MAX_PICKS, config.BEAR_MAX_PICKS,
        config.HOLD_DAYS_HINT_MIN, config.HOLD_DAYS_HINT_MAX)

    try:
        # Step 1/4 市场环境（供通知与数量上限使用；_screen_quality_pool 内部也会取，
        # 但走同一份内存 cache，不会产生额外请求）
        t = time.time()
        logger.info("Step 1/4 获取市场环境...")
        market_env = {}
        try:
            market_env = bfs.get_market_environment(config, cache)
            market_env_desc = market_env.get("description", "unknown")
        except Exception:  # noqa: BLE001 市场环境失败不阻断主流程
            market_env_desc = "获取失败"
            logger.warning("Step 1/4 市场环境获取失败（不影响主流程）:\n%s",
                           traceback.format_exc()[-500:])
        logger.info("Step 1/4 完成 (%.1f 秒): %s", time.time() - t, market_env_desc)

        # Step 2/4 执行长期口径筛选。
        # 直接复用 _screen_quality_pool（生产 quality_value 用的同一函数），
        # 通过 evaluator 参数注入长期评估器 —— 这是该函数为多口径预留的正式入口。
        t = time.time()
        logger.info("Step 2/4 执行长期优质低估低位筛选...")
        pending_rows: list[dict] = []
        volatile_rows: list[dict] = []
        index_df = bfs.get_index_daily(config, cache)
        output_df = bfs._screen_quality_pool(
            config=config,
            cache=cache,
            market_env=market_env,
            latest_trade_date=bfs.latest_completed_trade_day(config, cache),
            pending_out=pending_rows,
            evaluator=evaluate_quality_longterm,
            index_df=index_df,
            volatile_out=volatile_rows,
        )
        # _screen_quality_pool 不含市场熔断与 regime 数量收缩（那两步在 main() 里），
        # 长期 sleeve 同样必须受这两层组合风控保护，故在此显式补齐。
        # 先熔断后限量：顺序与 main() 一致，急跌中不因限量逻辑而漏掉熔断日志。
        halt = bfs.market_crash_halt(index_df, config)
        if halt is not None:
            logger.warning("市场级熔断：沪深300 近 %d 个交易日累计 %.2f%%（阈值 %.2f%%），"
                           "长期策略本次同样不推荐",
                           config.MARKET_CRASH_LOOKBACK, halt, config.MARKET_CRASH_HALT_PCT)
            output_df = None
        elif output_df is not None and not output_df.empty:
            regime = market_env.get("regime", "unknown")
            cap = bfs.resolve_max_picks(regime, config)
            if len(output_df) > cap:
                logger.info("市场环境 %s：长期策略数量上限 %d，按综合分截取前 %d 只（原始 %d 只）",
                            regime, cap, cap, len(output_df))
                output_df = output_df.head(cap).reset_index(drop=True)

        n_picks = 0 if output_df is None else len(output_df)
        logger.info("Step 2/4 完成 (%.1f 分钟)，正式推荐 %d 只，待核验候选 %d 只，波动率超限观察 %d 只",
                    (time.time() - t) / 60, n_picks, len(pending_rows), len(volatile_rows))
        for _, r in (output_df or pd.DataFrame()).iterrows():
            logger.info("  推荐: %s(%s) 评分 %s（排序分 %s）%s级 收盘 %s | 入选 %s | 持有 %d~%d 天",
                        r.get("name"), r.get("code"), r.get("score"),
                        "-" if pd.isna(r.get("rank_score")) else f"{float(r['rank_score']):.1f}",
                        r.get("grade"), r.get("close"), r.get("signals_hit") or "-",
                        r.get("holding_days_hint_min") or config.HOLD_DAYS_HINT_MIN,
                        r.get("holding_days_hint_max") or config.HOLD_DAYS_HINT_MAX)

        if pending_rows:
            logger.info("待核验候选 %d 只（数据不完整，仅日志留档，不上卡片、不落库）：%s",
                        len(pending_rows),
                        "，".join(f"{p.get('name')}({p.get('code')})" for p in pending_rows[:8]))
        if volatile_rows:
            logger.info("波动率观察池 %d 只（ATR 超限，仅日志留档，不上卡片）", len(volatile_rows))

        # Step 3/4 落库（strategy 必须是已注册值，否则 mysql_store 直接抛错）
        t = time.time()
        if not args.no_save:
            logger.info("Step 3/4 推荐结果落库 MySQL（strategy=%s）...", STRATEGY_LONGTERM_QUALITY)
            try:
                saved = save_recommendations(output_df, strategy=STRATEGY_LONGTERM_QUALITY)
                logger.info("Step 3/4 完成 (%.1f 秒)，写入 %d 条", time.time() - t, saved)
            except Exception as e:  # noqa: BLE001 落库失败不阻断通知
                logger.error("Step 3/4 落库失败（不影响通知）: %s", e)
        else:
            logger.info("Step 3/4 已跳过（--no-save）")

        # Step 4/4 飞书通知（口径声明与 run.py 同口径）
        if not args.no_notify:
            t = time.time()
            logger.info("Step 4/4 发送飞书通知...")
            degraded = is_data_degraded()
            if degraded:
                logger.warning("本次运行数据源已降级（Baostock 不可用，回退 AkShare）："
                               "估值字段与成长数据可能缺失，正式推荐数或被压低")
            vmode = _valuation_mode_of(output_df)
            if vmode in ("absolute", "mixed"):
                logger.warning("本次估值口径为 %s，飞书卡片将显式声明",
                               "绝对阈值回退" if vmode == "absolute" else "混合口径")
            notify_screening_result(
                output_df,
                market_env=market_env_desc,
                strategy=STRATEGY_LONGTERM_QUALITY,
                pending=pd.DataFrame(pending_rows) if pending_rows else None,
                volatile=pd.DataFrame(volatile_rows) if volatile_rows else None,
                data_degraded=degraded,
                valuation_mode=vmode,
                recommendation_mode=config.RECOMMENDATION_MODE,
            )
            logger.info("Step 4/4 完成 (%.1f 秒)", time.time() - t)
        else:
            logger.info("Step 4/4 已跳过（--no-notify）")

        if args.compare:
            _run_comparison(cache)

        logger.info("任务全部完成，总耗时 %.1f 分钟", (time.time() - t_start) / 60)

    except Exception as e:  # noqa: BLE001
        logger.exception("长期策略执行失败: %s", e)
        if not args.no_notify:
            notify_screening_result(
                None, market_env=market_env_desc, error_msg=str(e),
                strategy=STRATEGY_LONGTERM_QUALITY)
        sys.exit(1)


def _run_comparison(cache) -> None:
    """对比模式：用生产默认配置跑一遍 quality_value，打印两条口径的名单差异。

    只做口径对照，不落库、不通知——它的用途是人工判断「长期口径到底多筛掉了什么」，
    不应污染实盘数据。
    """
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        _screen_quality_pool,
        evaluate_quality_value,
        get_index_daily,
        get_market_environment,
        latest_completed_trade_day,
    )

    logger.info("=" * 60)
    logger.info("对比模式：运行生产 quality_value 口径（仅对照，不落库不推送）")
    logger.info("=" * 60)

    base = StrategyConfig()
    base_df = _screen_quality_pool(
        config=base,
        cache=cache,
        market_env=get_market_environment(base, cache),
        latest_trade_date=latest_completed_trade_day(base, cache),
        evaluator=evaluate_quality_value,
        index_df=get_index_daily(base, cache),
    )
    if base_df is None or base_df.empty:
        logger.info("生产 quality_value 口径本次无正式推荐")
        return
    logger.info("生产 quality_value 推荐 %d 只（持有 %d~%d 天）：", len(base_df),
                base.HOLD_DAYS_HINT_MIN, base.HOLD_DAYS_HINT_MAX)
    for idx, (_, row) in enumerate(base_df.iterrows(), 1):
        logger.info("%d. %s %s | 评分%.1f | 质量%.0f | 估值%.0f",
                    idx, row["code"], row["name"], row["score"],
                    row.get("quality_score", 0), row.get("valuation_score", 0))
    logger.info("=" * 60)
    logger.info("口径差异：长期版=%d年质量+ROE稳定性+5年现金流≥%.2f+资质≥%.0f+RS≥%.0fpp，持有30~90天；"
                "生产版=%d年质量，持有%d~%d天",
                base.QUALITY_YEARS, base.QUALITY_CASH_CONVERSION_MIN,
                base.HOLD_DAYS_HINT_MIN, base.HOLD_DAYS_HINT_MAX,
                base.QUALITY_YEARS, base.HOLD_DAYS_HINT_MIN, base.HOLD_DAYS_HINT_MAX)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
