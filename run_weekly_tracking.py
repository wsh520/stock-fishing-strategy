"""周度追踪脚本：统计推荐个股的最新表现，落库 MySQL 并飞书汇总。

追踪口径：
- 每条推荐记录自推荐日起最多追踪一个月（TRACK_MAX_WEEKS=4 次周度记录，
  且推荐日起 TRACK_MAX_AGE_DAYS=31 天后强制退出，双保险）；
- 每次记录当时的最新收盘价：收益值 = 最新收盘价 - 推荐时收盘价，
  收益率 = 收益值 / 推荐时收盘价 × 100%；
- 同一股票在不同日期被重复推荐的，视为不同推荐记录，各自独立追踪。

由 GitHub Actions 每周五收盘后执行（weekly_tracking.yml），也可本地手动：
    python run_weekly_tracking.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# 将 src 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd


def run():
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
        print("[INFO] MySQL 未配置（MYSQL_HOST/USER/PASSWORD/DATABASE），周度追踪退出")
        return

    # Step 1: 查询仍在追踪期内的推荐记录（一个月内、未达 4 次追踪）
    recs = get_active_recommendations()
    if not recs:
        print("[INFO] 无追踪期内的推荐记录")
        return
    print(f"[INFO] 待追踪推荐记录: {len(recs)} 条")

    # Step 2: 登录数据源（Baostock 主 / AkShare 备，与选股主流程同一套连接管理）
    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            print("[WARN] Baostock 登录失败，本次降级 AkShare 拉取行情")
            _bs_state["circuit_open"] = True
        else:
            print("[ERROR] Baostock 登录失败且未安装 AkShare，无法拉取行情，追踪终止")
            return

    config = StrategyConfig()
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
        except Exception:
            return rec, None, None

    # Step 3: 并发拉行情（bs_lock 保护 Baostock），主线程逐条落库
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
                    print(f"[WARN] {name}({code}) 行情获取失败，本次跳过")
                    continue

                # 行情未走出推荐日（如周任务与日任务同日执行）：收益恒为 0，
                # 跳过且不计入追踪次数，保证 4 次追踪都是有效观测
                rec_date_str = rec["rec_date"].strftime("%Y-%m-%d") if hasattr(rec["rec_date"], "strftime") else str(rec["rec_date"])
                if close_date <= rec_date_str:
                    print(f"[INFO] {name}({code}) 收盘价仍为推荐日({rec_date_str})，本次不计追踪")
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
                    print(f"[INFO] {name}({code}) 第{week_no}周: {rec['rec_close']} -> {close_price} ({ret_pct:+.2f}%)")
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

    print(f"[INFO] 周度追踪完成：成功 {tracked} 条，失败 {failed} 条")

    # Step 4: 飞书汇总通知（复用现有追踪报告卡片）
    if report_rows:
        notify_tracking_result(pd.DataFrame(report_rows))


if __name__ == "__main__":
    run()
