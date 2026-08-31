"""
回测引擎 — 对历史数据批量执行选股策略并模拟交易

功能：
1. 获取指定日期范围的全量周线/日线数据
2. 逐股扫描周线信号 + 日线确认（跳过基本面）
3. 模拟每笔交易的入场/退出（止损/止盈/到期）
4. 计算收益率统计（胜率、平均收益、盈亏比、等级分布等）
5. 结果存入 MySQL（backtest_trades + backtest_summary）

用法:
    python src/backtest.py                               # 默认 20260101 至今
    python src/backtest.py --start 20260101 --end 20260831
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# 确保 src/ 在路径中
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from src.bottom_fishing_strategy import (
    StrategyConfig,
    CacheManager,
    compute_weekly_signals,
    compute_daily_signals,
    compute_risk_reward,
    compute_market_environment,
    get_stock_list,
    _grade_from_score,
    fetch_weekly_range,
    fetch_daily_range,
    fetch_index_weekly_range,
)


# ===========================================================================
# BacktestConfig
# ===========================================================================


@dataclass
class BacktestConfig:
    """回测专用配置。"""

    start_date: str = "20260101"
    end_date: str = ""  # 空 = 今天
    max_hold_weeks: int = 4  # 最大持有周数
    weekly_lookback: int = 60  # 额外向前取的周数（供指标计算）
    daily_lookback: int = 60  # 信号前后取多少天日线
    max_concurrent_positions: int = 0  # 0 = 不限
    skip_fundamentals: bool = True  # 回测跳过基本面（避免前视偏差）
    max_workers: int = 4  # 并发获取数据线程数
    fetch_delay: float = 0.05  # API调用间隔（秒）
    incremental: bool = False  # 增量模式：自动从上次结束日期续跑


# ===========================================================================
# 退出模拟（纯函数）
# ===========================================================================


def simulate_exit(
    daily_df: pd.DataFrame,
    entry_date: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    max_hold_days: int,
) -> dict:
    """模拟单笔交易的退出。

    从 entry_date 次日起逐日检查：
    - 日最低 <= 止损价 → 按止损价退出
    - 日最高 >= 止盈价 → 按止盈价退出
    - 持有超 max_hold_days 日 → 按到期日收盘价退出

    Returns:
        {"exit_date", "exit_price", "return_pct", "hold_days", "exit_reason"}
    """
    if daily_df is None or daily_df.empty:
        return {
            "exit_date": entry_date,
            "exit_price": entry_price,
            "return_pct": 0.0,
            "hold_days": 0,
            "exit_reason": "no_data",
        }

    # 确保日期列可比较
    df = daily_df.copy()
    df["_date"] = pd.to_datetime(df["date"])
    entry_dt = pd.to_datetime(entry_date)

    # 取入场后的日线数据
    future = df[df["_date"] > entry_dt].sort_values("_date").reset_index(drop=True)

    if future.empty:
        return {
            "exit_date": entry_date,
            "exit_price": entry_price,
            "return_pct": 0.0,
            "hold_days": 0,
            "exit_reason": "no_future_data",
        }

    for i, row in future.iterrows():
        hold_days = (row["_date"] - entry_dt).days
        low = float(row.get("low", row["close"]))
        high = float(row.get("high", row["close"]))
        close = float(row["close"])
        current_date = row["_date"].strftime("%Y-%m-%d")

        # 止损优先
        if low <= stop_loss:
            ret = (stop_loss - entry_price) / entry_price * 100
            return {
                "exit_date": current_date,
                "exit_price": round(stop_loss, 2),
                "return_pct": round(ret, 2),
                "hold_days": hold_days,
                "exit_reason": "stop_loss",
            }

        # 止盈
        if high >= take_profit:
            ret = (take_profit - entry_price) / entry_price * 100
            return {
                "exit_date": current_date,
                "exit_price": round(take_profit, 2),
                "return_pct": round(ret, 2),
                "hold_days": hold_days,
                "exit_reason": "take_profit",
            }

        # 到期
        if hold_days >= max_hold_days:
            ret = (close - entry_price) / entry_price * 100
            return {
                "exit_date": current_date,
                "exit_price": round(close, 2),
                "return_pct": round(ret, 2),
                "hold_days": hold_days,
                "exit_reason": "expired",
            }

    # 数据不足以覆盖持有期 → 按最后一日收盘退出
    last = future.iloc[-1]
    hold_days = (last["_date"] - entry_dt).days
    close = float(last["close"])
    ret = (close - entry_price) / entry_price * 100
    return {
        "exit_date": last["_date"].strftime("%Y-%m-%d"),
        "exit_price": round(close, 2),
        "return_pct": round(ret, 2),
        "hold_days": hold_days,
        "exit_reason": "data_end",
    }


# ===========================================================================
# 单股回测处理
# ===========================================================================


def _process_stock(
    stock: dict,
    strategy_config: StrategyConfig,
    bt_config: BacktestConfig,
    data_start: str,
    data_end: str,
    signal_start: str,
    signal_end: str,
    market_envs: dict,
) -> list[dict]:
    """对单只股票执行回测，返回交易记录列表。"""
    code = stock["code"]
    name = stock.get("name", "")
    trades = []

    time.sleep(bt_config.fetch_delay)

    # 获取完整日期范围的周线数据（含 lookback）
    weekly_df = fetch_weekly_range(code, data_start, data_end, strategy_config)
    if weekly_df is None or len(weekly_df) < strategy_config.MIN_WEEKS:
        return trades

    # 获取完整日期范围的日线数据
    daily_df = fetch_daily_range(code, data_start, data_end, strategy_config)
    if daily_df is None or len(daily_df) < strategy_config.MIN_DAYS:
        return trades

    # 计算周线信号（对全量数据一次性计算）
    weekly_out = compute_weekly_signals(weekly_df, strategy_config)
    if weekly_out is None or weekly_out.empty:
        return trades

    # 筛选信号期间内触发的信号
    weekly_out["_date"] = pd.to_datetime(weekly_out["date"])
    signal_start_dt = pd.to_datetime(signal_start)
    signal_end_dt = pd.to_datetime(signal_end)
    signals = weekly_out[
        (weekly_out["weekly_signal"] == True)
        & (weekly_out["_date"] >= signal_start_dt)
        & (weekly_out["_date"] <= signal_end_dt)
    ]

    if signals.empty:
        return trades

    # 日线数据预处理
    daily_out = compute_daily_signals(daily_df, strategy_config)

    max_hold_days = bt_config.max_hold_weeks * 7

    for _, w_row in signals.iterrows():
        sig_date = w_row["_date"]
        sig_date_str = sig_date.strftime("%Y-%m-%d")

        # 日线确认：取信号日期附近的日线数据
        if daily_out is not None and not daily_out.empty:
            daily_out_copy = daily_out.copy()
            daily_out_copy["_date"] = pd.to_datetime(daily_out_copy["date"])
            # 取信号日期及之前的日线（最近一条）
            d_before = daily_out_copy[daily_out_copy["_date"] <= sig_date]
            if d_before.empty:
                continue
            d_last = d_before.iloc[-1]
            daily_score = float(d_last.get("daily_score", 0))
            daily_confirmed = bool(d_last.get("daily_signal", False))
            rsi_val = float(d_last.get("rsi14", 50))
        else:
            continue

        # 门槛：日线必须确认
        if not daily_confirmed:
            continue

        # 合并评分
        weekly_score = float(w_row["weekly_score"])
        total_score = weekly_score + daily_score

        # 市场环境：取最近的周市场环境
        week_key = sig_date.strftime("%Y-%W")
        env = market_envs.get(week_key, {})
        regime = env.get("regime", "unknown")
        grade_boost = strategy_config.BEAR_GRADE_BOOST if regime == "bear" else 0.0

        grade = _grade_from_score(total_score - grade_boost, strategy_config)
        if grade == "D":
            continue

        # 风险收益比
        entry_price = float(w_row["close"])
        atr_val = float(w_row.get("atr14", 0))
        ma20_val = float(w_row.get("ma20", 0))
        if atr_val <= 0:
            continue

        rr = compute_risk_reward(
            entry_price=entry_price,
            atr=atr_val,
            ma20=ma20_val,
            config=strategy_config,
        )
        if not rr["passes"]:
            continue

        # 模拟退出
        exit_result = simulate_exit(
            daily_df=daily_df,
            entry_date=sig_date_str,
            entry_price=entry_price,
            stop_loss=rr["stop_loss"],
            take_profit=rr["take_profit"],
            max_hold_days=max_hold_days,
        )

        trades.append({
            "code": code,
            "name": name,
            "entry_date": sig_date_str,
            "entry_price": round(entry_price, 2),
            "exit_date": exit_result["exit_date"],
            "exit_price": exit_result["exit_price"],
            "return_pct": exit_result["return_pct"],
            "hold_days": exit_result["hold_days"],
            "exit_reason": exit_result["exit_reason"],
            "score": round(total_score, 1),
            "grade": grade,
            "weekly_score": round(weekly_score, 1),
            "daily_score": round(daily_score, 1),
            "stop_loss": rr["stop_loss"],
            "take_profit": rr["take_profit"],
            "rr_ratio": rr["rr_ratio"],
            "market_env": regime,
        })

    return trades


# ===========================================================================
# 滚动市场环境计算
# ===========================================================================


def _compute_rolling_market_envs(
    strategy_config: StrategyConfig,
    data_start: str,
    data_end: str,
) -> dict:
    """计算每周的市场环境（CSI300 周线 MA20 斜率）。

    返回 {week_key: {"regime": ..., ...}} 的字典，week_key = "YYYY-WW"。
    """
    index_df = fetch_index_weekly_range(
        strategy_config.CSI300_AK_SYMBOL, data_start, data_end, strategy_config,
    )
    if index_df is None or index_df.empty:
        print("[WARN] 无法获取 CSI300 指数数据，市场环境默认 unknown")
        return {}

    envs = {}
    index_df["_date"] = pd.to_datetime(index_df["date"])

    for i in range(len(index_df)):
        # 每周使用截至当周的数据计算环境
        subset = index_df.iloc[: i + 1].copy()
        if len(subset) < strategy_config.MARKET_MA_PERIOD:
            continue
        env = compute_market_environment(subset.drop(columns=["_date"]), strategy_config)
        week_date = index_df.iloc[i]["_date"]
        week_key = week_date.strftime("%Y-%W")
        envs[week_key] = env

    return envs


# ===========================================================================
# 统计汇总
# ===========================================================================


def _compute_summary(trades_df: pd.DataFrame, bt_config: BacktestConfig) -> dict:
    """从交易记录 DataFrame 计算汇总统计。"""
    if trades_df.empty:
        return {
            "start_date": bt_config.start_date,
            "end_date": bt_config.end_date or datetime.now().strftime("%Y%m%d"),
            "total_trades": 0,
            "win_count": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_return": 0.0,
            "min_return": 0.0,
            "profit_factor": 0.0,
            "avg_hold_days": 0.0,
            "stop_loss_count": 0,
            "take_profit_count": 0,
            "expired_count": 0,
        }

    returns = trades_df["return_pct"].astype(float)
    total = len(returns)
    wins = (returns > 0).sum()
    losses = (returns <= 0).sum()

    # 盈亏比 = 总盈利 / |总亏损|
    total_profit = returns[returns > 0].sum()
    total_loss = abs(returns[returns <= 0].sum())
    profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
    if profit_factor == float("inf"):
        profit_factor = 999.99

    exit_reasons = trades_df["exit_reason"].value_counts().to_dict()

    return {
        "start_date": bt_config.start_date,
        "end_date": bt_config.end_date or datetime.now().strftime("%Y%m%d"),
        "total_trades": total,
        "win_count": int(wins),
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
        "avg_return": round(returns.mean(), 2),
        "max_return": round(returns.max(), 2),
        "min_return": round(returns.min(), 2),
        "profit_factor": round(profit_factor, 2),
        "avg_hold_days": round(trades_df["hold_days"].astype(float).mean(), 1),
        "stop_loss_count": exit_reasons.get("stop_loss", 0),
        "take_profit_count": exit_reasons.get("take_profit", 0),
        "expired_count": exit_reasons.get("expired", 0) + exit_reasons.get("data_end", 0),
    }


# ===========================================================================
# 增量回测：自动探测上次结束日期
# ===========================================================================


def _resolve_incremental_start(default_start: str) -> str:
    """查询上次回测的结束日期，作为本次增量回测的起始日期。

    优先级: MySQL → CSV 文件 → 使用默认 start_date（首次全量回测）。
    """
    # 1. 尝试从 MySQL 获取
    try:
        from db import get_mysql_store
        db = get_mysql_store()
        if db:
            last_end = db.get_last_backtest_end_date()
            if last_end:
                print(f"[回测] 增量模式: 从 MySQL 获取上次结束日期 {last_end}")
                return last_end
    except Exception:
        pass

    # 2. 尝试从 CSV 获取（扫描 data/backtest_summary_*.csv 中最新的 end_date）
    try:
        data_dir = os.path.join(_ROOT_DIR, "data")
        import glob
        csv_files = sorted(glob.glob(os.path.join(data_dir, "backtest_summary_*.csv")))
        if csv_files:
            latest_csv = csv_files[-1]
            df = pd.read_csv(latest_csv)
            if not df.empty and "end_date" in df.columns:
                last_end = str(df.iloc[-1]["end_date"]).replace("-", "")
                if len(last_end) == 8 and last_end.isdigit():
                    print(f"[回测] 增量模式: 从 CSV 获取上次结束日期 {last_end}")
                    return last_end
    except Exception:
        pass

    # 3. 无历史记录 → 首次全量回测
    print(f"[回测] 增量模式: 无历史记录，使用默认起始日期 {default_start}（首次全量回测）")
    return default_start


# ===========================================================================
# 主入口
# ===========================================================================


def run_backtest(
    bt_config: BacktestConfig = None,
    strategy_config: StrategyConfig = None,
) -> dict:
    """执行完整回测。

    Returns:
        {"backtest_id": str, "trades": pd.DataFrame, "summary": dict}
    """
    if bt_config is None:
        bt_config = BacktestConfig()
    if strategy_config is None:
        strategy_config = StrategyConfig()

    backtest_id = uuid.uuid4().hex[:16]
    end_date = bt_config.end_date or datetime.now().strftime("%Y%m%d")

    # 增量模式：自动探测起始日期
    if bt_config.incremental:
        resolved_start = _resolve_incremental_start(bt_config.start_date)
        bt_config.start_date = resolved_start

    # 数据范围：信号区间向前扩展 lookback
    signal_start = bt_config.start_date
    signal_end = end_date
    data_start_dt = datetime.strptime(signal_start, "%Y%m%d") - timedelta(weeks=bt_config.weekly_lookback)
    data_start = data_start_dt.strftime("%Y%m%d")
    # 数据结束日期向后延伸以覆盖最后一个信号的持有期
    data_end_dt = datetime.strptime(signal_end, "%Y%m%d") + timedelta(weeks=bt_config.max_hold_weeks + 1)
    data_end = data_end_dt.strftime("%Y%m%d")

    signal_start_fmt = f"{signal_start[:4]}-{signal_start[4:6]}-{signal_start[6:]}"
    signal_end_fmt = f"{signal_end[:4]}-{signal_end[4:6]}-{signal_end[6:]}"

    print(f"[回测] ID: {backtest_id}")
    print(f"[回测] 信号区间: {signal_start_fmt} ~ {signal_end_fmt}")
    print(f"[回测] 数据范围: {data_start} ~ {data_end}")
    print(f"[回测] 最大持有: {bt_config.max_hold_weeks} 周 ({bt_config.max_hold_weeks * 7} 天)")
    print(f"[回测] 跳过基本面: {bt_config.skip_fundamentals}")
    print()

    # Step 1: 获取股票列表
    print("[回测] Step 1/4: 获取股票列表...")
    cache = CacheManager(expire_hours=strategy_config.CACHE_EXPIRE_HOURS)
    stocks = get_stock_list(strategy_config, cache)
    if not stocks:
        print("[ERROR] 无法获取股票列表")
        return {"backtest_id": backtest_id, "trades": pd.DataFrame(), "summary": {}}
    print(f"[回测] 共 {len(stocks)} 只股票待扫描")

    # Step 2: 计算滚动市场环境
    print("[回测] Step 2/4: 计算市场环境...")
    market_envs = _compute_rolling_market_envs(strategy_config, data_start, data_end)
    print(f"[回测] 市场环境覆盖 {len(market_envs)} 周")

    # Step 3: 逐股扫描信号 + 模拟退出
    print(f"[回测] Step 3/4: 扫描信号并模拟交易 (workers={bt_config.max_workers})...")
    all_trades: list[dict] = []
    completed = 0
    total_stocks = len(stocks)

    with ThreadPoolExecutor(max_workers=bt_config.max_workers) as executor:
        futures = {}
        for stock in stocks:
            f = executor.submit(
                _process_stock,
                stock=stock,
                strategy_config=strategy_config,
                bt_config=bt_config,
                data_start=data_start,
                data_end=data_end,
                signal_start=signal_start_fmt,
                signal_end=signal_end_fmt,
                market_envs=market_envs,
            )
            futures[f] = stock["code"]

        for f in as_completed(futures):
            completed += 1
            code = futures[f]
            try:
                trades = f.result()
                if trades:
                    all_trades.extend(trades)
            except Exception as e:
                print(f"[WARN] {code} 回测异常: {e}")

            if completed % 100 == 0 or completed == total_stocks:
                print(f"[回测]   进度: {completed}/{total_stocks}, 累计信号: {len(all_trades)}")

    # Step 4: 统计
    print(f"\n[回测] Step 4/4: 计算统计...")
    trades_df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()
    summary = _compute_summary(trades_df, bt_config)
    summary["params_json"] = json.dumps(asdict(strategy_config), ensure_ascii=False, default=str)

    # 打印结果
    _print_report(trades_df, summary)

    # 保存到 MySQL
    try:
        from db import get_mysql_store
        db = get_mysql_store()
        if db:
            db.save_backtest_trades(backtest_id, trades_df)
            db.save_backtest_summary(backtest_id, summary)
            print(f"\n[回测] 结果已保存至 MySQL (backtest_id: {backtest_id})")
        else:
            print("\n[回测] MySQL 未配置，跳过数据库存储")
    except Exception as e:
        print(f"\n[WARN] MySQL 存储失败: {e}")

    # 保存到 CSV
    _save_csv(trades_df, summary, backtest_id)

    return {"backtest_id": backtest_id, "trades": trades_df, "summary": summary}


# ===========================================================================
# 报告输出
# ===========================================================================


def _print_report(trades_df: pd.DataFrame, summary: dict) -> None:
    """打印回测统计报告。"""
    print("\n" + "=" * 60)
    print("                    回测结果报告")
    print("=" * 60)

    if trades_df.empty:
        print("  未产生任何交易信号。")
        return

    total = summary["total_trades"]
    print(f"  信号区间: {summary['start_date']} ~ {summary['end_date']}")
    print(f"  总交易数: {total}")
    print(f"  盈利笔数: {summary['win_count']}")
    print(f"  胜    率: {summary['win_rate']:.1f}%")
    print(f"  平均收益: {summary['avg_return']:.2f}%")
    print(f"  最大收益: {summary['max_return']:.2f}%")
    print(f"  最大亏损: {summary['min_return']:.2f}%")
    print(f"  盈亏比  : {summary['profit_factor']:.2f}")
    print(f"  平均持有: {summary['avg_hold_days']:.1f} 天")
    print()
    print(f"  退出原因分布:")
    print(f"    止损退出: {summary['stop_loss_count']} 笔")
    print(f"    止盈退出: {summary['take_profit_count']} 笔")
    print(f"    到期退出: {summary['expired_count']} 笔")

    # 等级分布
    if not trades_df.empty and "grade" in trades_df.columns:
        print()
        print(f"  信号等级分布:")
        grade_stats = trades_df.groupby("grade").agg(
            count=("return_pct", "count"),
            avg_return=("return_pct", "mean"),
            win_rate=("return_pct", lambda x: (x > 0).mean() * 100),
        ).round(2)
        for grade in ["A", "B", "C"]:
            if grade in grade_stats.index:
                row = grade_stats.loc[grade]
                print(
                    f"    {grade}级: {int(row['count'])} 笔, "
                    f"胜率 {row['win_rate']:.1f}%, "
                    f"平均收益 {row['avg_return']:.2f}%"
                )

    # 月度分布
    if not trades_df.empty and "entry_date" in trades_df.columns:
        print()
        print(f"  月度信号分布:")
        trades_df_copy = trades_df.copy()
        trades_df_copy["month"] = pd.to_datetime(trades_df_copy["entry_date"]).dt.to_period("M")
        monthly = trades_df_copy.groupby("month").agg(
            count=("return_pct", "count"),
            avg_return=("return_pct", "mean"),
            win_rate=("return_pct", lambda x: (x > 0).mean() * 100),
        ).round(2)
        for month, row in monthly.iterrows():
            print(
                f"    {month}: {int(row['count'])} 笔, "
                f"胜率 {row['win_rate']:.1f}%, "
                f"平均收益 {row['avg_return']:.2f}%"
            )

    print("=" * 60)


def _save_csv(trades_df: pd.DataFrame, summary: dict, backtest_id: str) -> None:
    """保存回测结果到 CSV 文件。"""
    data_dir = os.path.join(_ROOT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)

    if not trades_df.empty:
        trades_path = os.path.join(data_dir, f"backtest_trades_{backtest_id}.csv")
        trades_df.to_csv(trades_path, index=False, encoding="utf-8-sig")
        print(f"[回测] 交易记录已保存: {trades_path}")

    summary_path = os.path.join(data_dir, f"backtest_summary_{backtest_id}.csv")
    summary_for_csv = {k: v for k, v in summary.items() if k != "params_json"}
    pd.DataFrame([summary_for_csv]).to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"[回测] 汇总统计已保存: {summary_path}")


# ===========================================================================
# CLI 入口
# ===========================================================================


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="选股策略回测引擎")
    parser.add_argument("--start", default="20260101", help="回测起始日期 YYYYMMDD (default: 20260101)")
    parser.add_argument("--end", default="", help="回测结束日期 YYYYMMDD (default: 今天)")
    parser.add_argument("--max-hold-weeks", type=int, default=4, help="最大持有周数 (default: 4)")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数 (default: 4)")
    parser.add_argument("--incremental", action="store_true",
                        help="增量模式: 自动从上次回测结束日期续跑（首次运行等同全量）")
    args = parser.parse_args()

    bt_config = BacktestConfig(
        start_date=args.start,
        end_date=args.end,
        max_hold_weeks=args.max_hold_weeks,
        max_workers=args.workers,
        incremental=args.incremental,
    )

    result = run_backtest(bt_config=bt_config)
    sys.exit(0 if result["summary"].get("total_trades", 0) >= 0 else 1)
