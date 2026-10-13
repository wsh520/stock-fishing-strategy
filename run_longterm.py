#!/usr/bin/env python3
"""
长期优质低估低位选股策略 - 执行入口

用法：
    python run_longterm.py                    # 正常运行
    python run_longterm.py --no-cache         # 跳过缓存
    python run_longterm.py --no-save          # 不落库（测试用）
    python run_longterm.py --no-notify        # 不推送飞书
    python run_longterm.py --compare          # 对比原策略与长期版差异

特点：
1. 更严格的质量筛选（5年 vs 3年）
2. 更合理的止跌判定（不强制站上MA20）
3. 更长的持有周期（30-90天 vs 10-20天）
4. 长期投资资质评分（ROE稳定性、现金流质量等）
"""

import argparse
import logging
import os
import sys
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.quality_longterm_strategy import LongTermQualityConfig, evaluate_quality_longterm
from src.bottom_fishing_strategy import (
    get_stock_list, get_index_daily, get_market_environment,
    run_concurrent_screen, _rank_signals, build_industry_valuation_snapshot,
    get_stock_industry, CacheManager, _tag_valuation_mode, _bs_login, _bs_logout
)
from store.mysql_store import save_recommendations
from notify.feishu import send_stock_recommendations

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("longterm")


def main_longterm(config: LongTermQualityConfig, no_cache: bool = False,
                 no_save: bool = False, no_notify: bool = False,
                 compare_mode: bool = False):
    """长期投资版主流程"""
    
    if not _bs_login():
        logger.error("Baostock登录失败")
        return
    
    try:
        cache = CacheManager(config.CACHE_EXPIRE_HOURS)
        if no_cache:
            cache.clear()
            logger.info("已清除内存缓存")
        
        # 1. 获取市场环境
        logger.info("=" * 60)
        logger.info("长期优质低估低位选股策略 - 开始执行")
        logger.info("=" * 60)
        
        index_df = get_index_daily(config, cache)
        market_env = get_market_environment(config, cache)
        regime = market_env.get("regime", "unknown")
        
        logger.info("市场环境: %s（沪深300 MA20斜率: %.2f%%）", 
                   regime, market_env.get("slope_pct", 0) * 100)
        
        # 2. 解析推荐数量上限
        if regime == "bull":
            max_picks = config.MAX_PICKS
        elif regime == "neutral":
            max_picks = config.NEUTRAL_MAX_PICKS
        else:  # bear or unknown
            max_picks = config.BEAR_MAX_PICKS
        
        logger.info("推荐数量上限: %d只（长期精选模式）", max_picks)
        
        # 3. 市场级熔断检查
        crash_pct = market_env.get("recent_crash_pct")
        crash_threshold = config.MARKET_CRASH_HALT_PCT
        if crash_pct is not None and crash_pct <= crash_threshold:
            logger.warning("市场级熔断触发：沪深300近%d日累计跌幅%.2f%% <= %.2f%%，本次不推荐",
                          config.MARKET_CRASH_LOOKBACK, crash_pct, crash_threshold)
            if not no_notify:
                send_stock_recommendations([], market_env, "longterm熊市空仓")
            return
        
        # 4. 获取股票池并构建行业估值快照
        stocks = get_stock_list(config, cache)
        logger.info("股票池: %d只", len(stocks))
        
        industry_map = get_stock_industry(config, cache)
        snapshot = build_industry_valuation_snapshot(config, cache, stocks)
        
        # 5. 并发筛选
        logger.info("开始筛选（使用长期投资版评估器）...")
        
        latest_trade_date = str(index_df.iloc[-1]["date"])[:10] if index_df is not None and not index_df.empty else None
        
        pending_out = []
        volatile_out = []
        
        results = run_concurrent_screen(
            config=config,
            cache=cache,
            stocks=stocks,
            evaluator=evaluate_quality_longterm,  # 使用长期版评估器
            market_env=market_env,
            latest_trade_date=latest_trade_date,
            snapshot=snapshot,
            industry_map=industry_map,
            index_df=index_df,
            pending_out=pending_out,
            volatile_out=volatile_out
        )
        
        if results is None or results.empty:
            logger.info("筛选完成：无符合条件的股票")
            if not no_notify:
                send_stock_recommendations([], market_env, "longterm无信号")
            return
        
        # 6. 排序和行业分散
        ranked = _rank_signals(results, config)
        
        # 行业分散
        if config.USE_INDUSTRY_DEDUP and industry_map:
            final = []
            industry_count = {}
            for _, row in ranked.iterrows():
                code = str(row["code"])
                ind = industry_map.get(code, "")
                count = industry_count.get(ind, 0)
                if count < config.MAX_PICKS_PER_INDUSTRY:
                    final.append(row)
                    industry_count[ind] = count + 1
                if len(final) >= max_picks:
                    break
            import pandas as pd
            ranked = pd.DataFrame(final) if final else ranked.iloc[:0]
        else:
            ranked = ranked.head(max_picks)
        
        # 7. 标注估值口径
        if snapshot and industry_map:
            ranked = _tag_valuation_mode(ranked, snapshot, industry_map, config)
        
        logger.info("=" * 60)
        logger.info("长期优质低估低位筛选结果: %d只正式推荐", len(ranked))
        logger.info("=" * 60)
        
        for idx, (_, row) in enumerate(ranked.iterrows(), 1):
            logger.info("%d. %s %s | 评分%.1f | 质量%.0f | 估值%.0f | 排序%.1f | PE%.1f | 250日位置%.0f%% | %s",
                       idx, row["code"], row["name"], row["score"], 
                       row.get("quality_score", 0), row.get("valuation_score", 0),
                       row.get("rank_score", 0), row.get("pe_ttm", 0),
                       row.get("position_250", 0) * 100,
                       row.get("signals_hit", "").split(",")[0])
        
        # 8. 打印待核验候选（仅日志，不推送）
        if pending_out:
            logger.info("待核验候选: %d只（数据不完整，仅供参考）", len(pending_out))
            for item in pending_out[:5]:
                logger.info("  - %s %s | 缺失: %s", 
                           item.get("code"), item.get("name"),
                           item.get("missing_tags", ""))
        
        # 9. 打印波动率观察池（仅日志）
        if volatile_out:
            logger.info("波动率观察池: %d只（ATR超限，风险较高）", len(volatile_out))
            for item in volatile_out[:3]:
                logger.info("  - %s %s | ATR%.2f%% > 上限%.1f%%",
                           item.get("code"), item.get("name"),
                           item.get("atr_pct", 0), item.get("limit", 0))
        
        # 10. 落库
        if not no_save and len(ranked) > 0:
            try:
                saved = save_recommendations(ranked, strategy="longterm_quality")
                logger.info("已保存%d条推荐记录到数据库（strategy=longterm_quality）", saved)
            except Exception as e:
                logger.error("保存到数据库失败: %s", e)
        
        # 11. 推送飞书
        if not no_notify and len(ranked) > 0:
            try:
                send_stock_recommendations(
                    ranked.to_dict("records"), 
                    market_env,
                    recommendation_mode="longterm"  # 自定义卡片标题
                )
                logger.info("已推送飞书通知")
            except Exception as e:
                logger.error("推送飞书失败: %s", e)
        
        # 12. 对比模式：与原策略对比
        if compare_mode:
            logger.info("=" * 60)
            logger.info("对比模式：运行原quality_value策略...")
            logger.info("=" * 60)
            compare_with_original(config, cache, market_env, stocks, 
                                 snapshot, industry_map, index_df,
                                 latest_trade_date, max_picks)
        
    finally:
        _bs_logout()
        logger.info("长期优质低估低位选股策略 - 执行完成")


def compare_with_original(config, cache, market_env, stocks, snapshot, 
                         industry_map, index_df, latest_trade_date, max_picks):
    """对比原策略与长期版的差异"""
    from src.bottom_fishing_strategy import evaluate_quality_value, StrategyConfig
    
    # 用原配置运行一遍
    original_config = StrategyConfig()
    original_config.USE_CACHE = config.USE_CACHE
    original_config.CACHE_DIR = config.CACHE_DIR
    
    pending_orig = []
    volatile_orig = []
    
    results_orig = run_concurrent_screen(
        config=original_config,
        cache=cache,
        stocks=stocks,
        evaluator=evaluate_quality_value,
        market_env=market_env,
        latest_trade_date=latest_trade_date,
        snapshot=snapshot,
        industry_map=industry_map,
        index_df=index_df,
        pending_out=pending_orig,
        volatile_out=volatile_orig
    )
    
    if results_orig is None or results_orig.empty:
        logger.info("原策略无推荐")
        return
    
    ranked_orig = _rank_signals(results_orig, original_config)
    
    # 行业分散
    if original_config.USE_INDUSTRY_DEDUP and industry_map:
        final = []
        industry_count = {}
        for _, row in ranked_orig.iterrows():
            code = str(row["code"])
            ind = industry_map.get(code, "")
            count = industry_count.get(ind, 0)
            if count < original_config.MAX_PICKS_PER_INDUSTRY:
                final.append(row)
                industry_count[ind] = count + 1
            if len(final) >= max_picks:
                break
        import pandas as pd
        ranked_orig = pd.DataFrame(final) if final else ranked_orig.iloc[:0]
    else:
        ranked_orig = ranked_orig.head(max_picks)
    
    logger.info("原策略(quality_value)推荐: %d只", len(ranked_orig))
    for idx, (_, row) in enumerate(ranked_orig.iterrows(), 1):
        logger.info("%d. %s %s | 评分%.1f | 持有10-20天",
                   idx, row["code"], row["name"], row["score"])
    
    logger.info("=" * 60)
    logger.info("差异分析:")
    logger.info("  长期版: 持有30-90天，5年质量，ROE稳定性，现金流≥1.0")
    logger.info("  原版:   持有10-20天，3年质量，止跌需站上MA20")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="长期优质低估低位选股策略")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存，强制重新拉取")
    parser.add_argument("--no-save", action="store_true", help="不保存到数据库")
    parser.add_argument("--no-notify", action="store_true", help="不推送飞书通知")
    parser.add_argument("--compare", action="store_true", help="对比原策略与长期版")
    
    args = parser.parse_args()
    
    config = LongTermQualityConfig()
    main_longterm(config, args.no_cache, args.no_save, args.no_notify, args.compare)
