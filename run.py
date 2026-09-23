"""
日线选股策略 GitHub Actions 入口脚本

功能：
1. 获取市场环境（用于通知）
2. 执行日线选股策略
3. 推荐信号落库 MySQL
4. 通过飞书 Webhook 发送结果通知
"""

import argparse
import logging
import sys
import os
import time
import traceback

import pandas as pd

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notify.feishu import (
    notify_screening_result,
)
from store.mysql_store import save_recommendations

logger = logging.getLogger("run")


def _setup_logging() -> None:
    """统一日志格式：时间戳 + 级别 + 模块名。GitHub Actions 上每行日志都带时间，方便定位卡点。"""
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
    同一天多次执行、或怀疑缓存被污染时使用。内存 TTL 缓存仍生效（同一次进程内复用）。

    --no-save / --no-notify：测试专用，跳过 MySQL 落库 / 飞书通知。
    实盘推荐表 stock_recommendation 以 (rec_date, code, strategy) 为唯一键且用
    INSERT IGNORE 写入——**不会清掉当天旧行**，因此白天测试写进去的推荐会与晚间
    正式结果并存在同一天，污染周度追踪与归因，还会干扰晚间突破策略的同股去重。
    纯测试请用这两个开关。
    """
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="日线选股策略入口脚本",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用 cache/ 磁盘缓存读写，强制从数据源拉最新数据",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="跳过 MySQL 落库（测试用，避免污染实盘推荐/追踪数据）",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="跳过飞书通知（测试用）",
    )
    return parser.parse_args(argv)


def _valuation_mode_of(df) -> str | None:
    """从策略层写入的 valuation_mode 列推断本次实际生效的估值口径。

    策略层（_screen_quality_pool → _tag_valuation_mode）已逐行标注 industry / absolute；
    这里收敛成一个卡片声明值，避免「口径悄悄回退到绝对阈值」在通知里没有任何痕迹。
    返回 None 表示无需声明（未启用行业相对估值，或拿不到该列）。
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


def run(argv: list[str] | None = None):
    """主执行流程"""
    _setup_logging()
    args = _parse_args(argv)
    save_enabled = not args.no_save
    notify_enabled = not args.no_notify
    # 延迟导入策略模块（确保路径已设置）
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        CacheManager,
        get_market_environment,
        is_data_degraded,
        main,
    )

    t_start = time.time()
    logger.info("=" * 60)
    logger.info("每日选股任务启动")
    if not save_enabled:
        logger.info("已启用 --no-save：本次不写 MySQL（纯测试运行，不污染实盘推荐与追踪数据）")
    if not notify_enabled:
        logger.info("已启用 --no-notify：本次不推送飞书通知")

    config = StrategyConfig()
    # 注：止跌确认闸门（QV_STABILIZATION_GATE）已收进 StrategyConfig 的库级默认值（True），
    # 本入口不再单独覆盖。此前旧名 QV_BEAR_TIMING_GATE 仅熊市生效，现扩展到全市场环境，
    # 配置项已重命名；旧名称通过 __post_init__ 自动迁移，不会静默改变含义。
    # 需要关掉时显式 config.QV_STABILIZATION_GATE = False。
    logger.info("生效口径：RECOMMENDATION_MODE=%s | 止跌确认闸门=%s | 综合分下限=%s",
                config.RECOMMENDATION_MODE, "启用" if config.QV_STABILIZATION_GATE else "关闭",
                config.MIN_QV_SCORE)
    if args.no_cache:
        config.USE_CACHE = False
        logger.info("已启用 --no-cache：跳过 cache/ 磁盘缓存读写，本次全部从数据源拉取")
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    market_env_desc = "unknown"

    try:
        # Step 1: 获取市场环境（用于通知）
        t = time.time()
        logger.info("Step 1/4 获取市场环境...")
        try:
            env_result = get_market_environment(config, cache)
            market_env_desc = env_result.get("description", "unknown")
        except Exception:
            market_env_desc = "获取失败"
            logger.warning("Step 1/4 市场环境获取失败（不影响主流程）:\n%s", traceback.format_exc()[-500:])
        logger.info("Step 1/4 完成 (%.1f 秒): %s", time.time() - t, market_env_desc)

        # Step 2: 运行选股策略（传入 config/cache 复用市场环境缓存）
        # pending_rows 收集「待核验候选」（财务/周线数据缺失），只用于通知展示，不落库、不参与追踪
        # volatile_rows 收集「波动率风控否决」（技术面已达标、仅 ATR 超限），只用于 CI 日志留档：
        # 这是「今天为什么没有推荐」最常见的原因，需要可逐只复核（**飞书不再推送**）
        t = time.time()
        logger.info("Step 2/4 执行选股策略（明细见 strategy 日志）...")
        pending_rows: list[dict] = []
        volatile_rows: list[dict] = []
        output_df = main(config=config, cache=cache, pending_out=pending_rows,
                         volatile_out=volatile_rows)
        n_picks = 0 if output_df is None else len(output_df)
        logger.info("Step 2/4 完成 (%.1f 分钟)，正式推荐 %d 只，待核验候选 %d 只，波动率超限观察 %d 只",
                    (time.time() - t) / 60, n_picks, len(pending_rows), len(volatile_rows))
        if output_df is not None and not output_df.empty:
            for _, r in output_df.iterrows():
                # 排序分 = 技术分 + 连续质量分，是决赛圈取 Top-N 的实际主键；
                # 与技术分并列打印，便于在 CI 日志里核对「为什么是这几只入选」
                _rs = r.get("rank_score")
                logger.info("  推荐: %s(%s) 评分 %s（排序分 %s）%s级 收盘 %s | 入选 %s | 核验 财务=%s 周线=%s",
                            r.get("name"), r.get("code"), r.get("score"),
                            "-" if _rs is None or pd.isna(_rs) else f"{float(_rs):.1f}",
                            r.get("grade"), r.get("close"),
                            r.get("signals_hit") or "-", r.get("fund_status") or "-", r.get("weekly_status") or "-")

        # Step 3: 推荐结果落库 MySQL（未配置环境变量时静默跳过，不影响主流程）
        t = time.time()
        if save_enabled:
            logger.info("Step 3/4 推荐结果落库 MySQL...")
            # 保底观察候选只用于通知/回测观察，不落库、不进入正式追踪归因
            formal_output_df = output_df
            if output_df is not None and not output_df.empty and "tier" in output_df.columns:
                formal_output_df = output_df[output_df["tier"].fillna("formal") == "formal"].copy()
            save_recommendations(formal_output_df, strategy="bottom_fishing")
            logger.info("Step 3/4 完成 (%.1f 秒)", time.time() - t)
        else:
            logger.info("Step 3/4 已跳过（--no-save）：测试结果不落库")

        # Step 4: 发送选股结果通知
        t = time.time()
        # P6：数据源降级可观测——Baostock 熔断/全程零命中时，AkShare 不提供逐 bar 估值，
        # quality_value 估值闸门缺列会把候选压成 pending，formal 可能为 0。显式标记，
        # 避免"数据降级导致的零推荐"被误读为"今天没有好票"。
        degraded = is_data_degraded()
        if degraded:
            logger.warning("本次运行数据源已降级（Baostock 不可用，回退 AkShare）："
                           "估值字段(peTTM/pbMRQ)可能缺失，正式推荐数或被压低，通知将标注数据状态")
        if notify_enabled:
            logger.info("Step 4/4 发送飞书通知...")
            # 飞书只发「通过全部筛选闸门的正式推荐 Top-3」：待核验候选（pending）与
            # 波动率否决（volatile）都只在上方 CI 日志留档、不上卡片——用户在群里看到的
            # 每一条都应是可直接执行的推荐，不含观察/备选标的。
            volatile_df = pd.DataFrame(volatile_rows) if volatile_rows else None
            # 估值口径声明：行业相对估值回退到绝对阈值时必须显式告知，否则用户会拿一份
            # 「口径已换」的名单与往常对比（绝对口径系统性偏向低 PE/PB 的传统板块）。
            vmode = _valuation_mode_of(output_df)
            if vmode in ("absolute", "mixed"):
                logger.warning("本次估值口径为 %s（行业相对估值未完全生效），飞书卡片将显式声明",
                               "绝对阈值回退" if vmode == "absolute" else "混合口径")
            notify_screening_result(output_df, market_env=market_env_desc,
                                    strategy="bottom_fishing", volatile=volatile_df,
                                    data_degraded=degraded, valuation_mode=vmode,
                                    recommendation_mode=config.RECOMMENDATION_MODE)
            logger.info("Step 4/4 完成 (%.1f 秒)", time.time() - t)
        else:
            logger.info("Step 4/4 已跳过（--no-notify）：不推送飞书")

        logger.info("任务全部完成，总耗时 %.1f 分钟", (time.time() - t_start) / 60)

    except Exception as e:
        logger.exception("策略执行失败: %s", e)
        # 发送错误通知（--no-notify 时同样跳过，避免测试运行刷屏；失败详情见上方 traceback）
        if notify_enabled:
            notify_screening_result(None, market_env=market_env_desc, error_msg=str(e),
                                    recommendation_mode=config.RECOMMENDATION_MODE)
        sys.exit(1)


if __name__ == "__main__":
    run()
