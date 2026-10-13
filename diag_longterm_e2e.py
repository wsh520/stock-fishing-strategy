#!/usr/bin/env python3
"""长期策略端到端模拟（离线）：验证 _screen_quality_pool 两趟流程真的能出票。

这是修复前完全跑不通的路径——预筛趟就会把每只股票按年度财务缺失否决。
本脚本把取数与财务全部打桩，只保留真实的筛选编排，输出实际归因分布。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import src.bottom_fishing_strategy as bfs
from src.quality_longterm_strategy import LongTermQualityConfig, evaluate_quality_longterm
from tests.test_longterm_pipeline_offline import (
    _bars, _fundamentals, _qualified_bars, _qualified_index)


def run_once(config, evaluator, label):
    daily = _qualified_bars()
    index = _qualified_index()
    last_day = str(daily["date"].iloc[-1])

    bfs.get_stock_list = lambda cfg, cache=None: [{"code": "600000", "name": "测试股"}]
    bfs.get_stock_industry = lambda cfg, cache=None: {"600000": "测试行业"}
    bfs.build_industry_valuation_snapshot = lambda cfg, cache, stocks: {}
    bfs.get_daily_data = lambda code, cfg, cache=None: daily
    # 负债率必须给值：缺失会被记 fund_debt 并降级 pending（数据缺失≠筛选通过）
    bfs.get_fundamentals = lambda code, cache=None, cfg=None: {"debt_ratio": 42.0}
    bfs.get_market_environment = lambda cfg, cache=None: {"regime": "neutral", "description": "中性"}

    def _enrich_annual(code, fund, cfg, as_of=None):
        return {**(fund or {}), "annual_rows": _fundamentals()}

    bfs.enrich_annual_fundamentals = _enrich_annual
    bfs.enrich_recent_operating = lambda code, fund, cfg, cache, as_of: fund
    bfs.enrich_financial_risk = lambda code, fund, cfg, cache, as_of: fund
    bfs.enrich_forward_growth = lambda code, fund, cfg, cache=None: fund

    pending, volatile = [], []
    df = bfs._screen_quality_pool(
        config=config, cache=bfs.CacheManager(1.0),
        market_env={"regime": "neutral"}, latest_trade_date=last_day,
        pending_out=pending, evaluator=evaluator, max_picks=3,
        index_df=index, volatile_out=volatile)
    n = 0 if df is None else len(df)
    print(f"\n[{label}] 正式推荐 {n} 只 / 待核验 {len(pending)} / 波动率观察 {len(volatile)}")
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            print(f"    {r['code']} {r['name']} | 评分 {r['score']} | tier={r['tier']} | "
                  f"持有 {r.get('holding_days_hint_min')}~{r.get('holding_days_hint_max')} | "
                  f"入选 {str(r.get('signals_hit'))[:70]}")
    return n


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True)

    lt = LongTermQualityConfig(USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
                               REQUIRE_FINANCIAL_RISK=False, REQUIRE_FORWARD_CONFIRMATION=False,
                               USE_INDUSTRY_RELATIVE_VALUATION=False, REQUIRE_HISTORICAL_VALUATION=False,
                               ENABLE_LONGTERM_QUALITY_SCORE=False)
    n_lt = run_once(lt, evaluate_quality_longterm, "长期 sleeve")

    prod = bfs.StrategyConfig(USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
                              REQUIRE_FINANCIAL_RISK=False, REQUIRE_FORWARD_CONFIRMATION=False,
                              USE_INDUSTRY_RELATIVE_VALUATION=False, REQUIRE_HISTORICAL_VALUATION=False)
    n_prod = run_once(prod, bfs.evaluate_quality_value, "生产 quality_value")

    print("\n" + "=" * 60)
    print(f"端到端结果：长期 {n_lt} 只 / 生产 {n_prod} 只")
    assert n_lt > 0, "长期 sleeve 仍然 0 推荐 —— 预筛误杀可能未真正修复"
    print("PASS：长期策略能正常产出信号")
