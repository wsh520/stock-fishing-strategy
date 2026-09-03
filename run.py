"""
日线选股策略 GitHub Actions 入口脚本

功能：
1. 执行日线选股策略
2. 保存推荐信号（CSV）
3. 执行周度追踪
4. 通过飞书 Webhook 发送结果通知
"""

import sys
import os
import traceback

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notify.feishu import (
    notify_screening_result,
)

def run():
    """主执行流程"""
    # 延迟导入策略模块（确保路径已设置）
    from src.bottom_fishing_strategy import (
        StrategyConfig,
        CacheManager,
        get_market_environment,
        main,
    )

    config = StrategyConfig()
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    market_env_desc = "unknown"


    try:
        # Step 1: 获取市场环境（用于通知）
        try:
            env_result = get_market_environment(config, cache)
            market_env_desc = env_result.get("description", "unknown")
        except Exception:
            market_env_desc = "获取失败"

        # Step 2: 运行选股策略（传入 config/cache 复用市场环境缓存）
        print("[INFO] 开始执行选股策略...")
        output_df = main(config=config, cache=cache)

        if output_df is not None and not output_df.empty:
            print(f"[INFO] 本次推荐 {len(output_df)} 只股票")
        else:
            print("[INFO] 本次无推荐信号")

        # Step 4: 发送选股结果通知
        notify_screening_result(output_df, market_env=market_env_desc)

        print("[INFO] 策略执行完成")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()[-500:]}"
        print(f"[ERROR] 策略执行失败: {error_msg}")
        # 发送错误通知
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
