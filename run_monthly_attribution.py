"""信号归因月报脚本：基于 stock_recommendation + stock_tracking 历史数据，
统计各信号维度（等级/底背离/市场环境/持有周次）的推荐后表现，飞书推送。

这不是回测——只统计已落库的真实推荐与真实追踪收益，用运行数据反哺信号质量评估：
- 总体收益只使用推荐后20交易日观测，不混合不同成熟度；
- 各策略分别按5/10/15/20交易日期限统计；旧数据未知期限单独计数排除；
- 峰值为已完成20日推荐在固定期限观测中的最高收益，不代表每日最大浮盈。

由 GitHub Actions 每月 1 日执行（monthly_attribution.yml），也可本地手动：
    python run_monthly_attribution.py
    python run_monthly_attribution.py --days 60   # 自定义统计窗口（天，默认 90）
"""

import argparse
import logging
import sys
import time

import pandas as pd

logger = logging.getLogger("attribution")

_DEFAULT_DAYS = 90


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
    parser = argparse.ArgumentParser(
        prog="run_monthly_attribution.py",
        description="信号归因月报：统计各信号维度的推荐后表现并推送飞书",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=_DEFAULT_DAYS,
        help=f"统计窗口（天），只归因该窗口内的推荐记录，默认 {_DEFAULT_DAYS}",
    )
    return parser.parse_args(argv)


def _group_stats(df: pd.DataFrame, key: str, labels: dict | None = None) -> list[dict]:
    """按 key 列分组统计：样本数 / 最新收益胜率 / 平均最新收益 / 平均峰值收益。

    labels: 分组展示名映射（如 {1: "有底背离", 0: "无底背离"}）；缺省直接用组值。
    每行是「一条推荐」level 的数据（return_pct=最新周收益，peak_pct=期间峰值收益）。
    """
    rows = []
    for val, sub in df.groupby(key, observed=True):
        label = (labels or {}).get(val, str(val))
        n = len(sub)
        win = int((sub["return_pct"] > 0).sum())
        rows.append({
            "label": label,
            "n": int(n),
            "win_rate": round(win / n * 100, 1) if n else 0.0,
            "avg_return": round(float(sub["return_pct"].mean()), 2),
            "avg_peak": round(float(sub["peak_pct"].mean()), 2),
        })
    return rows


def build_stats(rows: list[dict], days: int) -> dict:
    """明细行 → 归因统计 dict（供 notify_attribution_report 消费）。"""
    df = pd.DataFrame(rows, columns=list(dict.fromkeys(
        ["id", "rec_date", "close_date", "holding_trade_days", "strategy", "week_no",
         "return_pct", "has_divergence", "grade", "market_env", "daily_score"]
        + [key for row in rows for key in row])))
    df["strategy"] = df["strategy"].fillna("bottom_fishing")
    df["holding_trade_days"] = pd.to_numeric(df["holding_trade_days"], errors="coerce")
    df["rec_date"] = pd.to_datetime(df["rec_date"], errors="coerce")
    df["close_date"] = pd.to_datetime(df["close_date"], errors="coerce")
    unknown = int((~df["holding_trade_days"].isin([5, 10, 15, 20])).sum())
    df = df[df["holding_trade_days"].isin([5, 10, 15, 20])
            & (df["close_date"] > df["rec_date"])].copy()
    if "legacy_duplicate" in df:
        df = df[df["legacy_duplicate"].fillna(0).eq(0)]
    if "tracking_id" in df:
        df = df.sort_values("tracking_id")
    df = df.drop_duplicates(["id", "close_date"]).drop_duplicates(["id", "holding_trade_days"])
    df["return_pct"] = pd.to_numeric(df["return_pct"], errors="coerce")
    df = df[df["return_pct"].notna() & df["return_pct"].abs().lt(float("inf"))]
    df["week_no"] = df["holding_trade_days"] // 5
    df["return_pct"] = df["return_pct"].astype(float)
    df["week_no"] = df["week_no"].astype(int)
    df["has_divergence"] = df["has_divergence"].astype(int)

    # 每条推荐一行：信号字段取首条（同推荐恒定），收益取最新周，峰值取全部周最大值
    latest = df[df["holding_trade_days"].eq(20)].set_index("id")
    peak = df.groupby("id")["return_pct"].max().rename("peak_pct")
    weeks = df.groupby("id")["week_no"].max().rename("weeks")
    recs = latest.join(peak).join(weeks).reset_index()

    n = len(recs)
    win = int((recs["return_pct"] > 0).sum())
    stats = {
        "period": f"近{days}天 · 推荐后20交易日",
        "horizon_trade_days": 20,
        "excluded_unknown_horizon": unknown,
        "tracked_recs": n,
        "win_rate": round(win / n * 100, 1) if n else 0.0,
        "avg_return": round(float(recs["return_pct"].mean()), 2) if n else 0.0,
        "avg_peak": round(float(recs["peak_pct"].mean()), 2) if n else 0.0,
        "by_grade": _group_stats(recs, "grade"),
        "by_divergence": _group_stats(recs, "has_divergence", {1: "有底背离", 0: "无底背离"}),
        "by_market": _group_stats(recs, "market_env", {"bull": "牛市", "bear": "熊市", "neutral": "中性", "unknown": "未知"}),
    }

    # 持有周次效应：所有观测按周次分组（第1周=推荐后首周），看收益随持有期的变化趋势
    by_week = []
    for (strategy, horizon), sub in df.groupby(["strategy", "holding_trade_days"]):
        m = len(sub)
        win_w = int((sub["return_pct"] > 0).sum())
        by_week.append({
            "label": f"{strategy} · {int(horizon)}交易日",
            "strategy": strategy,
            "holding_trade_days": int(horizon),
            "n": int(m),
            "win_rate": round(win_w / m * 100, 1) if m else 0.0,
            "avg_return": round(float(sub["return_pct"].mean()), 2),
            "avg_peak": round(float(sub["return_pct"].max()), 2),
        })
    stats["by_week"] = by_week
    stats["by_strategy_horizon"] = by_week
    stats["by_strategy"] = _group_stats(recs, "strategy")

    # 评分分档（用日线基础分，不受底背离升档影响；熊市加码下可能出现 <60 的基础分）
    recs["score_bucket"] = pd.cut(
        recs["daily_score"].astype(float), bins=[0, 60, 80, 1000],
        labels=["60分以下", "60-80分", "80分以上"],
    )
    stats["by_score"] = _group_stats(recs.dropna(subset=["score_bucket"]), "score_bucket")
    return stats


def run(argv: list[str] | None = None):
    _setup_logging()
    args = _parse_args(argv)
    t_start = time.time()
    logger.info("=" * 60)
    logger.info("信号归因月报任务启动（统计窗口 %d 天）", args.days)

    from store.mysql_store import get_attribution_rows, is_configured
    from notify.feishu import notify_attribution_report

    if not is_configured():
        logger.info("MySQL 未配置（MYSQL_HOST/USER/PASSWORD/DATABASE），归因月报退出")
        return

    logger.info("Step 1/2 查询推荐与追踪明细...")
    rows = get_attribution_rows(days=args.days)
    if not rows:
        logger.info("近 %d 天无已追踪的推荐记录，任务结束", args.days)
        return
    logger.info("Step 1/2 完成：%d 条周度观测明细", len(rows))

    logger.info("Step 2/2 聚合归因统计并发送飞书通知...")
    stats = build_stats(rows, args.days)
    logger.info(
        "整体：已追踪推荐 %d 条，胜率 %.1f%%，平均收益 %.2f%%，平均峰值 %.2f%%",
        stats["tracked_recs"], stats["win_rate"], stats["avg_return"], stats["avg_peak"],
    )
    for title, key in (("等级", "by_grade"), ("底背离", "by_divergence"),
                       ("市场环境", "by_market"), ("持有周次", "by_week"), ("评分分档", "by_score")):
        for g in stats[key]:
            logger.info("  [%s] %s: %d 只 | 胜率 %.1f%% | 平均 %.2f%% | 峰值 %.2f%%",
                        title, g["label"], g["n"], g["win_rate"], g["avg_return"], g["avg_peak"])

    notify_attribution_report(stats)
    logger.info("任务全部完成，总耗时 %.1f 秒", time.time() - t_start)


if __name__ == "__main__":
    run()
