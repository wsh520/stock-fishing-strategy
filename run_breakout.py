"""
放量突破选股策略 GitHub Actions / 本地入口脚本

功能（对齐 run.py 的四步流水线）：
1. 获取市场环境（用于通知 + 熊市空仓决策）
2. 执行放量突破选股策略
3. 推荐信号落库 MySQL（复用 stock_recommendation，保持现有追踪链路）
4. 通过飞书 Webhook 发送结果通知

使用方式：
    python run_breakout.py            # 正常执行（命中当日缓存）
    python run_breakout.py --no-cache # 跳过磁盘缓存，强制从数据源拉最新数据

与 run.py 的差异：
- 导入 volume_breakout_strategy 而非 bottom_fishing_strategy
- 熊市直接跳过（main_breakout 内部已处理，本脚本仅记录日志）
- 落库复用 save_recommendations；同日同股重复推荐由现有唯一键忽略
- 通知标题为「放量突破选股结果」，与抄底策略通知区分
"""

import argparse
import logging
import os
import sys
import time
import traceback

import pandas as pd

# 将项目根目录加入 Python 路径（确保 src 包可导入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notify.feishu import notify_screening_result
from store.mysql_store import save_recommendations

logger = logging.getLogger("run_breakout")


def _setup_logging() -> None:
    """统一日志格式：时间戳 + 级别 + 模块名。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数解析。

    --no-cache：跳过磁盘缓存读写（cache/ 目录），强制从 Baostock/AkShare 拉最新数据。
    --no-save：跳过 MySQL 落库（仅飞书通知，适合验证信号质量阶段）。
    --no-notify：跳过飞书通知（仅落库，适合本地调试）。
    """
    parser = argparse.ArgumentParser(
        prog="run_breakout.py",
        description="放量突破选股策略入口脚本",
    )
    parser.add_argument("--no-cache", action="store_true",
                        help="禁用 cache/ 磁盘缓存读写，强制从数据源拉最新数据")
    parser.add_argument("--no-save", action="store_true",
                        help="跳过 MySQL 落库（仅飞书通知）")
    parser.add_argument("--no-notify", action="store_true",
                        help="跳过飞书通知（仅落库）")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None):
    """主执行流程"""
    _setup_logging()
    args = _parse_args(argv)

    from src.volume_breakout_strategy import (
        VolumeBreakoutConfig,
        CacheManager,
        get_market_environment,
        main_breakout,
    )

    t_start = time.time()
    logger.info("=" * 60)
    logger.info("放量突破选股任务启动")

    config = VolumeBreakoutConfig()
    # ===== 突破策略＝独立的第二信号源（#1）=====
    # 抄底入口（run.py）跑 quality_value（优质低估低位）。若突破入口也用 quality_value，
    # evaluate_breakout 会委派同一个 evaluate_quality_value 资格判定，两套策略产出完全相同，
    # 再经下方组合层去重后突破卡片恒为空——等于花双份成本拿一份结果。
    # 这里强制 technical 模式，让突破走自己独立的「七层漏斗」（突破/量能/形态/平台/趋势/
    # 假突破/RSI/动能/波动率/评分 + 决赛圈周线确认 + 熊市空仓），成为真正独立的信号源；
    # 与抄底的重叠由下方 fetch_rec_codes_for_date 去重、合计上限由 DAILY_TOTAL_MAX_PICKS 约束。
    config.RECOMMENDATION_MODE = "technical"
    if args.no_cache:
        config.USE_CACHE = False
        logger.info("已启用 --no-cache：跳过 cache/ 磁盘缓存读写，本次全部从数据源拉取")
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    market_env_desc = "unknown"

    try:
        # Step 1: 获取市场环境（用于通知 + 熊市空仓决策）
        t = time.time()
        logger.info("Step 1/4 获取市场环境...")
        try:
            env_result = get_market_environment(config, cache)
            market_env_desc = env_result.get("description", "unknown")
            regime = env_result.get("regime", "unknown")
            # 熊市直接跳过（main_breakout 内部也会跳过，此处提前记录）
            if regime == "bear" and config.RECOMMENDATION_MODE == "technical":
                logger.info("市场环境为 bear，放量突破策略直接空仓（BEAR_MAX_PICKS_BREAKOUT=0），本次运行终止")
                if not args.no_notify:
                    # 必须显式传 strategy：notify_screening_result 的默认值是 bottom_fishing，
                    # 漏传会让熊市空仓通知套用抄底模板（标题/措辞串味）。该分支在 bear 时必然命中。
                    notify_screening_result(None, market_env=market_env_desc, strategy="volume_breakout")
                return
        except Exception:
            market_env_desc = "获取失败"
            logger.warning("Step 1/4 市场环境获取失败（不影响主流程）:\n%s", traceback.format_exc()[-500:])
        logger.info("Step 1/4 完成 (%.1f 秒): %s", time.time() - t, market_env_desc)

        # Step 2: 运行放量突破选股策略
        # volatile_rows 收集「波动率风控否决」（突破/量能/形态/趋势各层已过、仅 ATR 超限），
        # 只用于日志与飞书高风险观察池，不落库、不参与追踪与归因
        t = time.time()
        logger.info("Step 2/4 执行放量突破选股策略（明细见 strategy.breakout 日志）...")
        volatile_rows: list[dict] = []
        pending_rows: list[dict] = []
        output_df = main_breakout(config=config, cache=cache, volatile_out=volatile_rows,
                                  pending_out=pending_rows)
        logger.info("Step 2/4 完成 (%.1f 分钟)，推荐 %d 只，波动率超限观察 %d 只", (time.time() - t) / 60,
                    0 if output_df is None else len(output_df), len(volatile_rows))

        # 组合层：同一交易日已被其他策略推荐的个股不再重复推荐（同股去重），
        # 且两策略合计推荐数不超过 DAILY_TOTAL_MAX_PICKS（依赖运行顺序：后运行者去重）。
        if output_df is not None and not output_df.empty:
            try:
                from store.mysql_store import fetch_rec_codes_for_date, is_configured as _ms_configured
                if _ms_configured():
                    sig_date = str(output_df.iloc[0].get("date", ""))
                    existing = fetch_rec_codes_for_date(sig_date) if sig_date else set()
                    if existing:
                        before = len(output_df)
                        output_df = output_df[~output_df["code"].astype(str).isin(existing)].reset_index(drop=True)
                        if len(output_df) < before:
                            logger.info("组合层去重：%d 只今日已被其他策略推荐，剔除后剩 %d 只",
                                        before - len(output_df), len(output_df))
                    cap = int(getattr(config, "DAILY_TOTAL_MAX_PICKS", 7))
                    room = max(0, cap - len(existing))
                    if len(output_df) > room:
                        logger.info("组合层总量上限：今日已推 %d 只，本策略截取前 %d 只（合计 ≤%d）",
                                    len(existing), room, cap)
                        output_df = output_df.head(room).reset_index(drop=True)
            except Exception as e:
                logger.warning("组合层去重/上限检查失败（按原结果继续）: %s", e)

        n_picks = 0 if output_df is None else len(output_df)
        if output_df is not None and not output_df.empty:
            for _, r in output_df.iterrows():
                logger.info("  推荐: %s(%s) 评分 %s %s级 L%s 突破幅度 %s%% 收盘 %s",
                            r.get("name"), r.get("code"), r.get("score"), r.get("grade"),
                            r.get("breakout_level"), r.get("breakout_margin"), r.get("close"))

        # Step 3: 推荐结果落库 MySQL。stock_recommendation 的唯一键为
        # (rec_date, code, strategy)，同一股票同日可被两套策略分别推荐并存。
        if not args.no_save:
            t = time.time()
            logger.info("Step 3/4 推荐结果落库 MySQL...")
            try:
                save_recommendations(output_df, strategy="volume_breakout")
            except Exception as e:
                logger.warning("Step 3/4 落库失败（不影响通知）: %s", e)
            logger.info("Step 3/4 完成 (%.1f 秒)", time.time() - t)
        else:
            logger.info("Step 3/4 已跳过（--no-save）")

        # Step 4: 发送选股结果通知
        if not args.no_notify:
            t = time.time()
            logger.info("Step 4/4 发送飞书通知...")
            # 待核验候选（pending）仅在上方 CI 日志中打印计数，不再进入飞书卡片：
            # 数据不全的标的既不构成推荐也不该被误读为备选，飞书只展示正式推荐 Top-3。
            volatile_df = pd.DataFrame(volatile_rows) if volatile_rows else None
            notify_screening_result(output_df, market_env=market_env_desc,
                                    strategy="volume_breakout", volatile=volatile_df)
            logger.info("Step 4/4 完成 (%.1f 秒)", time.time() - t)
        else:
            logger.info("Step 4/4 已跳过（--no-notify）")

        logger.info("任务全部完成，总耗时 %.1f 分钟", (time.time() - t_start) / 60)

    except Exception as e:
        logger.exception("放量突破策略执行失败: %s", e)
        if not args.no_notify:
            # 同熊市分支：异常通知也要带 strategy，否则卡片标题回落为抄底口径
            notify_screening_result(None, market_env=market_env_desc, error_msg=str(e),
                                    strategy="volume_breakout")
        sys.exit(1)


if __name__ == "__main__":
    run()
