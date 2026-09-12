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
    return parser.parse_args(argv)


def run(argv: list[str] | None = None):
    """主执行流程"""
    _setup_logging()
    args = _parse_args(argv)
    # 延迟导入策略模块（确保路径已设置）
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        CacheManager,
        get_market_environment,
        main,
    )

    t_start = time.time()
    logger.info("=" * 60)
    logger.info("每日选股任务启动")

    config = StrategyConfig()
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
        t = time.time()
        logger.info("Step 2/4 执行选股策略（明细见 strategy 日志）...")
        pending_rows: list[dict] = []
        output_df = main(config=config, cache=cache, pending_out=pending_rows)
        n_picks = 0 if output_df is None else len(output_df)
        logger.info("Step 2/4 完成 (%.1f 分钟)，正式推荐 %d 只，待核验候选 %d 只",
                    (time.time() - t) / 60, n_picks, len(pending_rows))
        if output_df is not None and not output_df.empty:
            for _, r in output_df.iterrows():
                logger.info("  推荐: %s(%s) 评分 %s %s级 收盘 %s | 入选 %s | 核验 财务=%s 周线=%s",
                            r.get("name"), r.get("code"), r.get("score"), r.get("grade"), r.get("close"),
                            r.get("signals_hit") or "-", r.get("fund_status") or "-", r.get("weekly_status") or "-")

        # Step 3: 推荐结果落库 MySQL（未配置环境变量时静默跳过，不影响主流程）
        t = time.time()
        logger.info("Step 3/4 推荐结果落库 MySQL...")
        save_recommendations(output_df)
        logger.info("Step 3/4 完成 (%.1f 秒)", time.time() - t)

        # Step 4: 发送选股结果通知
        t = time.time()
        logger.info("Step 4/4 发送飞书通知...")
        pending_df = pd.DataFrame(pending_rows) if pending_rows else None
        notify_screening_result(output_df, market_env=market_env_desc, pending=pending_df)
        logger.info("Step 4/4 完成 (%.1f 秒)", time.time() - t)

        logger.info("任务全部完成，总耗时 %.1f 分钟", (time.time() - t_start) / 60)

    except Exception as e:
        logger.exception("策略执行失败: %s", e)
        # 发送错误通知
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
