"""
选股策略 GitHub Actions 入口脚本

功能：
1. 增量同步当日 K 线数据到 DB（MySQL 可用时）
2. 执行选股策略（优先从 DB 读取数据）
3. 保存推荐信号（CSV + MySQL）
4. 执行周度追踪
5. 计算近一个月绩效统计（胜率/涨跌幅）
6. 通过飞书 Webhook 发送结果通知
"""

import sys
import os
import traceback

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notify.feishu import (
    notify_screening_result,
    notify_tracking_result,
    notify_monthly_performance,
)
from db import get_mysql_store


def run():
    """主执行流程"""
    # 延迟导入策略模块（确保路径已设置）
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        CacheManager,
        get_market_environment,
        main,
        save_signal_history,
        weekly_performance_review,
        update_signal_status,
    )

    config = StrategyConfig()
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    market_env_desc = "unknown"

    # 初始化 MySQL 存储（未配置时返回 None，不影响主流程）
    db = get_mysql_store()
    if db:
        print("[INFO] MySQL 存储已连接")
    else:
        print("[INFO] MySQL 未配置或不可用，仅使用 CSV 存储")

    try:
        # Step 0: 增量同步当日数据到 DB（MySQL 可用时自动执行）
        if db:
            try:
                from src.sync_data import run_sync
                print("[INFO] 开始增量同步当日数据...")
                sync_result = run_sync(full=False, max_workers=config.MAX_WORKERS)
                if "error" not in sync_result:
                    print(
                        f"[INFO] 数据同步完成: "
                        f"日线 {sync_result.get('daily_rows', 0)} 行, "
                        f"周线 {sync_result.get('weekly_rows', 0)} 行, "
                        f"耗时 {sync_result.get('elapsed_seconds', 0):.1f}s"
                    )
                else:
                    print(f"[WARN] 数据同步异常: {sync_result.get('error')}")
            except Exception as e:
                print(f"[WARN] 数据同步失败（不影响选股）: {e}")

        # Step 1: 获取市场环境（用于通知）
        try:
            env_result = get_market_environment(config, cache)
            market_env_desc = env_result.get("description", "unknown")
        except Exception:
            market_env_desc = "获取失败"

        # Step 2: 运行选股策略（传入 config/cache 复用市场环境缓存）
        print("[INFO] 开始执行选股策略...")
        output_df = main(config=config, cache=cache)

        # Step 3: 保存推荐信号
        if output_df is not None and not output_df.empty:
            save_signal_history(output_df)  # CSV 持久化
            print(f"[INFO] 本次推荐 {len(output_df)} 只股票")
            # MySQL 存储
            if db:
                db.save_recommendations(output_df)
        else:
            print("[INFO] 本次无推荐信号")

        # Step 4: 发送选股结果通知
        notify_screening_result(output_df, market_env=market_env_desc)

        # Step 5: 执行周度追踪
        print("[INFO] 执行周度追踪...")
        report = weekly_performance_review(config)
        if report is not None:
            update_signal_status(report)  # CSV 更新
            notify_tracking_result(report)
            # MySQL 更新追踪状态
            if db:
                db.update_tracking(report)

        # Step 6: 月度绩效报告（基于 MySQL 数据）
        if db:
            print("[INFO] 生成近30天绩效报告...")
            monthly_stats = db.get_monthly_performance(days=30)
            if monthly_stats and monthly_stats.get("total_signals", 0) > 0:
                db.save_performance_snapshot(monthly_stats)
                notify_monthly_performance(monthly_stats)
                print(
                    f"[INFO] 月度绩效: "
                    f"推荐{monthly_stats['total_signals']}只, "
                    f"胜率{monthly_stats['win_rate']:.1f}%, "
                    f"平均收益{monthly_stats['avg_return']:.2f}%"
                )
            else:
                print("[INFO] 近30天无推荐记录或无追踪数据，跳过绩效报告")

        print("[INFO] 策略执行完成")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()[-500:]}"
        print(f"[ERROR] 策略执行失败: {error_msg}")
        # 发送错误通知
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
