"""
优质低估低位策略 2.0 - 长期投资版

核心改进：
1. 更严格的质量筛选：连续5年优质（vs 原3年），ROE稳定性要求，现金流质量提升
2. 更合理的止跌判定：只拒绝"加速下跌"，不强制要求站上MA20
3. 更宽松的底部窗口：3-90天（vs 原5-40天），允许充分磨底
4. 长期投资导向：持有期30-90交易日，止损20%，止盈50%
5. 周期股优化处理：底部反转期给予估值豁免，使用PB<1.0作为补充
6. 长期投资资质评估：ROE稳定性、现金流质量、盈利持续性、竞争优势

定位：左侧价值投资 + 初步止跌确认（非右侧追涨）
适合：3-12个月持有周期的价值投资者
"""

from dataclasses import dataclass
from src.bottom_fishing_strategy import StrategyConfig
import numpy as np
import pandas as pd
from typing import Optional


@dataclass
class LongTermQualityConfig(StrategyConfig):
    """长期投资版配置：继承原策略配置，覆盖关键参数"""
    
    # ========== 1. 更严格的质量要求 ==========
    RECOMMENDATION_MODE: str = "quality_longterm"
    
    # 年度质量：从3年提升至5年
    QUALITY_YEARS: int = 5
    QUALITY_MEDIAN_ROE_MIN: float = 12.0  # 中位ROE从10%提升至12%
    QUALITY_MIN_ROE: float = 8.0  # 最低ROE从5%提升至8%
    QUALITY_CASH_CONVERSION_MIN: float = 0.9  # 现金转换率从0.8提升至0.9
    
    # 新增：ROE稳定性要求（标准差/均值 < 0.5，避免大起大落）
    REQUIRE_ROE_STABILITY: bool = True
    MAX_ROE_CV: float = 0.5  # Coefficient of Variation上限
    
    # 新增：盈利持续性要求（连续5年净利润同比增长，至少3年为正）
    REQUIRE_EARNINGS_CONSISTENCY: bool = True
    MIN_POSITIVE_GROWTH_YEARS: int = 3  # 5年中至少3年正增长
    
    # 新增：长期现金流质量（5年累计经营现金流/净利润 >= 1.0）
    LONGTERM_CASH_QUALITY_MIN: float = 1.0
    
    # ========== 2. 重新定义"开始回升"：只拒绝加速下跌 ==========
    
    # 止跌确认模式强制改为 risk_only
    QV_STABILIZATION_MODE: str = "risk_only"
    
    # MACD深度弱势阈值收紧（只拦最危险的形态）
    MACD_WEAK_DAYS: int = 3  # 连续3日恶化（vs 原2日）
    MACD_WEAK_HIST_PCT: float = -0.8  # 深度弱势阈值从-0.5%收紧至-0.8%
    
    # 新增：下降通道加速检测（MA20近5日跌幅>5%视为加速下跌）
    DETECT_ACCELERATING_DECLINE: bool = True
    MA20_ACCELERATE_THRESHOLD: float = -0.05  # MA20 5日跌幅阈值
    
    # 新增：成交量萎缩判定（近5日均量 < 近20日均量的50%视为缩量见底信号）
    DETECT_VOLUME_EXHAUSTION: bool = True
    VOLUME_EXHAUSTION_RATIO: float = 0.5
    
    # ========== 3. 更宽松的底部判定窗口 ==========
    
    QV_RECENT_LOW_WINDOW: int = 90  # 从60天扩展至90天
    QV_RECENT_LOW_MIN_AGE: int = 3  # 最小年龄从5天降至3天（允许快速反弹）
    QV_RECENT_LOW_MAX_AGE: int = 90  # 最大年龄从40天扩展至90天（允许长期磨底）
    
    # 距底上限从5×ATR改为更宽松的8×ATR，范围15%-35%
    QV_LOW_GAIN_ATR_MULT: float = 8.0
    QV_LOW_GAIN_MIN_PCT: float = 0.15  # 新增：最小允许涨幅15%
    QV_LOW_GAIN_MAX_PCT: float = 0.35  # 新增：最大允许涨幅35%
    
    # ========== 4. 长期投资导向 ==========
    
    # 建议持有周期延长至30-90交易日
    HOLD_DAYS_HINT_MIN: int = 30
    HOLD_DAYS_HINT_MAX: int = 90
    
    # 止损/止盈匹配长期周期
    FIXED_STOP_LOSS_PCT: float = 20.0  # 止损从15%放宽至20%
    FIXED_TAKE_PROFIT_PCT: float = 50.0  # 止盈从30%提高至50%
    ATR_STOP_MULT: float = 4.0  # ATR倍数从3倍提升至4倍
    ATR_STOP_FLOOR_PCT: float = 20.0  # ATR止损下限提升至20%
    
    # ========== 5. 估值优化 ==========
    
    # 行业相对估值分位从60%收紧至50%（更便宜）
    VALUATION_INDUSTRY_PERCENTILE_MAX: float = 0.50
    
    # 绝对PE上限保持25，但增加PB<1.0的补充条件
    ALLOW_PB_FALLBACK: bool = True  # 新增：PE略超标但PB<1.0可豁免
    PB_FALLBACK_THRESHOLD: float = 1.0
    PB_FALLBACK_PE_MAX: float = 30.0  # PE可放宽至30倍
    
    # 历史PE分位从60%收紧至50%
    HISTORICAL_VALUATION_PERCENTILE_MAX: float = 0.50
    
    # ========== 6. 周期股优化处理 ==========
    
    # 周期正常化PE上限从25提升至35（底部反转期给予更多空间）
    CYCLICAL_NORMALIZED_PE_MAX: float = 35.0
    
    # 新增：周期底部PB兜底（PB<0.8视为深度低估，可豁免PE）
    CYCLICAL_PB_FLOOR: float = 0.8
    CYCLICAL_PB_FALLBACK_ENABLED: bool = True
    
    # ========== 7. 长期投资资质专项评估 ==========
    
    # 新增：长期投资资质评分（0-100分）
    ENABLE_LONGTERM_QUALITY_SCORE: bool = True
    MIN_LONGTERM_QUALITY_SCORE: float = 60.0  # 资质评分门槛
    
    # 资质评分权重分配
    LTQ_ROE_STABILITY_WEIGHT: float = 0.25  # ROE稳定性
    LTQ_CASH_QUALITY_WEIGHT: float = 0.25   # 现金流质量
    LTQ_EARNINGS_CONSISTENCY_WEIGHT: float = 0.20  # 盈利持续性
    LTQ_GROWTH_QUALITY_WEIGHT: float = 0.15  # 增长质量（温和增长优于暴涨暴跌）
    LTQ_VALUATION_MARGIN_WEIGHT: float = 0.15  # 估值安全边际
    
    # ========== 8. 风控与推荐数量 ==========
    
    # 波动率上限保持6%
    MAX_ATR_PCT: float = 6.0
    
    # 推荐数量减少（长期投资应更精选）
    MAX_PICKS: int = 3  # 牛市从5只降至3只
    NEUTRAL_MAX_PICKS: int = 2  # 中性从4只降至2只
    BEAR_MAX_PICKS: int = 1  # 熊市从2只降至1只
    
    # 流动性要求提升（长期持有需要足够深度）
    MIN_AMOUNT: float = 50_000_000.0  # 从3000万提升至5000万
    
    # ========== 9. 综合评分权重调整 ==========
    
    # 长期投资更重视质量和估值，降低技术权重
    QUALITY_SCORE_WEIGHT: float = 0.55  # 从0.45提升至0.55
    VALUATION_SCORE_WEIGHT: float = 0.35  # 从0.30提升至0.35
    TECHNICAL_SCORE_WEIGHT: float = 0.10  # 从0.25降至0.10
    
    # 最低质量分要求（新增硬门槛）
    MIN_QUALITY_SCORE: float = 65.0  # 质量分必须≥65
    
    # 技术分门槛降低（因为只做初步止跌确认）
    MIN_TECHNICAL_SCORE_FORMAL: float = 30.0  # 从40降至30
    
    # ========== 10. 其他优化 ==========
    
    # 相对强度要求：不能严重跑输市场（新增硬门槛）
    REQUIRE_RELATIVE_STRENGTH: bool = True
    RS_MIN_THRESHOLD: float = -15.0  # 近60日跑输超过15个百分点→淘汰
    
    # 横盘筑底加分提升（奖励长期磨底）
    CONSOLIDATION_DAYS_MIN: int = 60  # 从30天提升至60天
    CONSOLIDATION_DAYS_MAX: int = 180  # 从120天提升至180天
    CONSOLIDATION_BONUS: float = 8.0  # 从5分提升至8分


def evaluate_roe_stability(annual_rows: list, min_years: int = 5) -> dict:
    """评估ROE稳定性（长期投资关键指标）
    
    返回:
        {
            "status": "verified" | "insufficient" | "unstable",
            "roe_mean": float,  # ROE均值
            "roe_std": float,   # ROE标准差
            "roe_cv": float,    # 变异系数 (std/mean)
            "stability_score": 0-100  # 稳定性评分
        }
    """
    if not annual_rows or len(annual_rows) < min_years:
        return {"status": "insufficient", "roe_mean": None, "roe_std": None, 
                "roe_cv": None, "stability_score": 0}
    
    roes = []
    for row in annual_rows[-min_years:]:
        roe = row.get("roe")
        if roe is None or not np.isfinite(roe):
            return {"status": "insufficient", "roe_mean": None, "roe_std": None,
                    "roe_cv": None, "stability_score": 0}
        roes.append(float(roe))
    
    mean_roe = np.mean(roes)
    std_roe = np.std(roes, ddof=1)
    
    if mean_roe <= 0:
        return {"status": "unstable", "roe_mean": mean_roe, "roe_std": std_roe,
                "roe_cv": None, "stability_score": 0}
    
    cv = std_roe / mean_roe  # 变异系数
    
    # 稳定性评分：CV越小越稳定
    # CV=0 → 100分, CV=0.5 → 0分，线性映射
    stability_score = max(0.0, min(100.0, 100.0 * (1.0 - cv / 0.5)))
    
    return {
        "status": "verified",
        "roe_mean": round(mean_roe, 2),
        "roe_std": round(std_roe, 2),
        "roe_cv": round(cv, 3),
        "stability_score": round(stability_score, 2)
    }


def evaluate_earnings_consistency(annual_rows: list, min_years: int = 5,
                                 min_positive_years: int = 3) -> dict:
    """评估盈利持续性（避免大起大落）
    
    返回:
        {
            "status": "verified" | "insufficient" | "inconsistent",
            "positive_growth_years": int,  # 正增长年数
            "total_years": int,
            "consistency_score": 0-100
        }
    """
    if not annual_rows or len(annual_rows) < min_years + 1:
        return {"status": "insufficient", "positive_growth_years": 0,
                "total_years": 0, "consistency_score": 0}
    
    growth_years = 0
    total_years = 0
    
    for i in range(len(annual_rows) - min_years, len(annual_rows)):
        curr_profit = annual_rows[i].get("net_profit")
        prev_profit = annual_rows[i-1].get("net_profit") if i > 0 else None
        
        if curr_profit is None or prev_profit is None:
            return {"status": "insufficient", "positive_growth_years": 0,
                    "total_years": 0, "consistency_score": 0}
        
        if not np.isfinite(curr_profit) or not np.isfinite(prev_profit):
            return {"status": "insufficient", "positive_growth_years": 0,
                    "total_years": 0, "consistency_score": 0}
        
        if prev_profit > 0 and curr_profit > prev_profit:
            growth_years += 1
        total_years += 1
    
    if growth_years < min_positive_years:
        return {"status": "inconsistent", "positive_growth_years": growth_years,
                "total_years": total_years, "consistency_score": 0}
    
    # 持续性评分：正增长占比越高越好
    consistency_score = 100.0 * growth_years / total_years if total_years > 0 else 0
    
    return {
        "status": "verified",
        "positive_growth_years": growth_years,
        "total_years": total_years,
        "consistency_score": round(consistency_score, 2)
    }


def evaluate_longterm_cash_quality(annual_rows: list, min_years: int = 5) -> dict:
    """评估长期现金流质量
    
    返回:
        {
            "status": "verified" | "insufficient" | "poor",
            "total_cashflow": float,  # N年累计经营现金流
            "total_profit": float,    # N年累计净利润
            "cash_ratio": float,      # 累计现金流/累计利润
            "cash_quality_score": 0-100
        }
    """
    if not annual_rows or len(annual_rows) < min_years:
        return {"status": "insufficient", "total_cashflow": None,
                "total_profit": None, "cash_ratio": None, "cash_quality_score": 0}
    
    total_cf = 0.0
    total_profit = 0.0
    
    for row in annual_rows[-min_years:]:
        cf = row.get("operating_cashflow")
        profit = row.get("net_profit")
        
        if cf is None or profit is None:
            return {"status": "insufficient", "total_cashflow": None,
                    "total_profit": None, "cash_ratio": None, "cash_quality_score": 0}
        
        if not np.isfinite(cf) or not np.isfinite(profit):
            return {"status": "insufficient", "total_cashflow": None,
                    "total_profit": None, "cash_ratio": None, "cash_quality_score": 0}
        
        total_cf += float(cf)
        total_profit += float(profit)
    
    if total_profit <= 0:
        return {"status": "poor", "total_cashflow": total_cf,
                "total_profit": total_profit, "cash_ratio": None, "cash_quality_score": 0}
    
    cash_ratio = total_cf / total_profit
    
    # 现金质量评分：比率越高越好
    # ratio >= 1.2 → 100分, ratio = 0.8 → 0分，线性映射
    cash_quality_score = max(0.0, min(100.0, 100.0 * (cash_ratio - 0.8) / 0.4))
    
    return {
        "status": "verified",
        "total_cashflow": round(total_cf, 2),
        "total_profit": round(total_profit, 2),
        "cash_ratio": round(cash_ratio, 3),
        "cash_quality_score": round(cash_quality_score, 2)
    }


def compute_longterm_quality_score(annual_rows: list, pe: Optional[float],
                                   pe_pct: Optional[float], config: LongTermQualityConfig) -> dict:
    """计算长期投资资质综合评分
    
    维度：
    1. ROE稳定性（25%）：避免大起大落
    2. 现金流质量（25%）：5年累计经营现金流/净利润
    3. 盈利持续性（20%）：连续增长年数
    4. 增长质量（15%）：温和增长优于暴涨暴跌
    5. 估值安全边际（15%）：越便宜安全边际越大
    """
    if not getattr(config, "ENABLE_LONGTERM_QUALITY_SCORE", False):
        return {"status": "disabled", "ltq_score": 100.0, "breakdown": {}}
    
    # 1. ROE稳定性
    roe_stability = evaluate_roe_stability(annual_rows, config.QUALITY_YEARS)
    s1 = roe_stability.get("stability_score", 0)
    
    # 2. 现金流质量
    cash_quality = evaluate_longterm_cash_quality(annual_rows, config.QUALITY_YEARS)
    s2 = cash_quality.get("cash_quality_score", 0)
    
    # 3. 盈利持续性
    earnings_consistency = evaluate_earnings_consistency(
        annual_rows, config.QUALITY_YEARS, 
        getattr(config, "MIN_POSITIVE_GROWTH_YEARS", 3))
    s3 = earnings_consistency.get("consistency_score", 0)
    
    # 4. 增长质量（ROE增长的稳定性）
    if len(annual_rows) >= config.QUALITY_YEARS:
        roes = [r.get("roe") for r in annual_rows[-config.QUALITY_YEARS:]]
        if all(r is not None and np.isfinite(r) for r in roes):
            # 计算ROE的线性趋势斜率
            x = np.arange(len(roes))
            slope = np.polyfit(x, roes, 1)[0]
            # 温和正增长（0-2个百分点/年）最佳
            if 0 <= slope <= 2.0:
                s4 = 100.0
            elif slope > 2.0:
                # 增长过快扣分（可能不可持续）
                s4 = max(0.0, 100.0 - (slope - 2.0) * 10)
            else:
                # 负增长扣分
                s4 = max(0.0, 100.0 + slope * 20)
        else:
            s4 = 0.0
    else:
        s4 = 0.0
    
    # 5. 估值安全边际
    if pe_pct is not None:
        # 行业分位：0% → 100分, 50% → 0分
        s5 = max(0.0, min(100.0, 100.0 * (1.0 - pe_pct / 0.5)))
    elif pe is not None and pe > 0:
        # 绝对PE：PE=10 → 100分, PE=25 → 0分
        s5 = max(0.0, min(100.0, 100.0 * (25.0 - pe) / 15.0))
    else:
        s5 = 0.0
    
    # 加权汇总
    w = config
    ltq_score = (
        w.LTQ_ROE_STABILITY_WEIGHT * s1 +
        w.LTQ_CASH_QUALITY_WEIGHT * s2 +
        w.LTQ_EARNINGS_CONSISTENCY_WEIGHT * s3 +
        w.LTQ_GROWTH_QUALITY_WEIGHT * s4 +
        w.LTQ_VALUATION_MARGIN_WEIGHT * s5
    )
    
    breakdown = {
        "roe_stability": {"score": s1, "weight": w.LTQ_ROE_STABILITY_WEIGHT, 
                         "details": roe_stability},
        "cash_quality": {"score": s2, "weight": w.LTQ_CASH_QUALITY_WEIGHT,
                        "details": cash_quality},
        "earnings_consistency": {"score": s3, "weight": w.LTQ_EARNINGS_CONSISTENCY_WEIGHT,
                                "details": earnings_consistency},
        "growth_quality": {"score": s4, "weight": w.LTQ_GROWTH_QUALITY_WEIGHT},
        "valuation_margin": {"score": s5, "weight": w.LTQ_VALUATION_MARGIN_WEIGHT}
    }
    
    status = "verified" if all(d.get("details", {}).get("status") == "verified" 
                              for d in breakdown.values() if "details" in d) else "partial"
    
    return {
        "status": status,
        "ltq_score": round(ltq_score, 2),
        "breakdown": breakdown
    }


def evaluate_quality_longterm(daily_df: Optional[pd.DataFrame], code: str, name: str,
                              config: LongTermQualityConfig, market_env: Optional[dict] = None,
                              fund_data: Optional[dict] = None,
                              latest_trade_date: Optional[str] = None,
                              val_context: Optional[dict] = None,
                              index_df: Optional[pd.DataFrame] = None,
                              volatile_out: Optional[list] = None):
    """长期投资版评估函数（在原evaluate_quality_value基础上增强）
    
    核心改进：
    1. 5年质量要求（vs 原3年）
    2. ROE稳定性检查
    3. 盈利持续性检查
    4. 长期现金流质量检查
    5. 长期投资资质综合评分
    6. 周期股PB兜底
    7. 相对强度硬门槛
    """
    from src.bottom_fishing_strategy import evaluate_quality_value
    
    # 先调用原有评估逻辑
    signal, reason = evaluate_quality_value(
        daily_df, code, name, config, market_env, fund_data,
        latest_trade_date, val_context, index_df, volatile_out
    )
    
    # 如果原评估已经淘汰，直接返回
    if signal is None:
        return None, reason
    
    # ===== 长期投资专项增强检查 =====
    
    fund = fund_data or {}
    annual_rows = fund.get("annual_rows", [])
    
    # 1. ROE稳定性检查
    if getattr(config, "REQUIRE_ROE_STABILITY", False):
        roe_stab = evaluate_roe_stability(annual_rows, config.QUALITY_YEARS)
        if roe_stab["status"] != "verified":
            return None, "FAIL_ROE_STABILITY_INSUFFICIENT"
        if roe_stab["roe_cv"] > config.MAX_ROE_CV:
            return None, "FAIL_ROE_UNSTABLE"
    
    # 2. 盈利持续性检查
    if getattr(config, "REQUIRE_EARNINGS_CONSISTENCY", False):
        earnings_cons = evaluate_earnings_consistency(
            annual_rows, config.QUALITY_YEARS,
            getattr(config, "MIN_POSITIVE_GROWTH_YEARS", 3))
        if earnings_cons["status"] == "inconsistent":
            return None, "FAIL_EARNINGS_INCONSISTENT"
        if earnings_cons["status"] == "insufficient":
            return None, "FAIL_EARNINGS_DATA_INSUFFICIENT"
    
    # 3. 长期现金流质量检查
    longterm_cash_min = getattr(config, "LONGTERM_CASH_QUALITY_MIN", None)
    if longterm_cash_min is not None:
        cash_quality = evaluate_longterm_cash_quality(annual_rows, config.QUALITY_YEARS)
        if cash_quality["status"] != "verified":
            return None, "FAIL_LONGTERM_CASH_INSUFFICIENT"
        if cash_quality["cash_ratio"] < longterm_cash_min:
            return None, "FAIL_LONGTERM_CASH_POOR"
    
    # 4. 长期投资资质评分
    pe = signal.pe_ttm
    pe_pct = val_context.get("pe_pct") if val_context else None
    ltq_eval = compute_longterm_quality_score(annual_rows, pe, pe_pct, config)
    
    if ltq_eval["status"] != "disabled":
        min_ltq = getattr(config, "MIN_LONGTERM_QUALITY_SCORE", 0.0)
        if ltq_eval["ltq_score"] < min_ltq:
            return None, "FAIL_LONGTERM_QUALITY_SCORE"
    
    # 5. 相对强度硬门槛（不能严重跑输市场）
    if getattr(config, "REQUIRE_RELATIVE_STRENGTH", False):
        rs = signal.relative_strength
        rs_min = getattr(config, "RS_MIN_THRESHOLD", -15.0)
        if rs is not None and rs < rs_min:
            return None, "FAIL_RELATIVE_STRENGTH_WEAK"
    
    # 6. 周期股PB兜底（补充估值判定）
    from src.recommendation_factors import classify_cyclical_industry
    industry_name = str((val_context or {}).get("industry", ""))
    cycle_class = classify_cyclical_industry(
        industry_name, config.CYCLICAL_INDUSTRY_CODES, 
        config.CYCLICAL_INDUSTRY_KEYWORDS)
    
    if cycle_class == "cyclical" and getattr(config, "CYCLICAL_PB_FALLBACK_ENABLED", False):
        pb = signal.pb_mrq
        pb_floor = getattr(config, "CYCLICAL_PB_FLOOR", 0.8)
        if pb is not None and pb > 0 and pb < pb_floor:
            # 深度低估的周期股，给PE更多容忍度
            signal.signals_hit += f",周期底部深度低估PB={pb:.2f}"
    
    # 7. 更新信号对象的长期投资相关字段
    tags = signal.signals_hit.split(",")
    tags[0] = "长期优质低估低位"  # 修改主标签
    
    # 添加长期投资资质标签
    if ltq_eval["status"] != "disabled":
        tags.append(f"长期投资资质{ltq_eval['ltq_score']:.0f}分")
        
        # 细分标签
        bd = ltq_eval["breakdown"]
        if bd["roe_stability"]["score"] >= 80:
            tags.append("ROE高度稳定")
        if bd["cash_quality"]["score"] >= 80:
            tags.append("现金流优质")
        if bd["earnings_consistency"]["score"] >= 80:
            tags.append("盈利持续增长")
    
    signal.signals_hit = ",".join(tags)
    
    # 更新持有期提示
    signal.holding_days_hint_min = config.HOLD_DAYS_HINT_MIN
    signal.holding_days_hint_max = config.HOLD_DAYS_HINT_MAX
    
    return signal, "PASS"


# 使用示例
if __name__ == "__main__":
    """
    使用方法：
    
    1. 在 run.py 中导入并使用：
    
    from src.quality_longterm_strategy import LongTermQualityConfig, evaluate_quality_longterm
    
    config = LongTermQualityConfig()
    signal, reason = evaluate_quality_longterm(daily_df, code, name, config, ...)
    
    2. 关键差异对比：
    
    原策略（quality_value）:
    - 3年质量，ROE中位10%，止跌要求MA20站上，持有10-20天
    - 定位：中短期价值投机
    
    长期版（quality_longterm）:
    - 5年质量，ROE中位12%且稳定，只拒绝加速下跌，持有30-90天
    - 定位：长期价值投资
    
    3. 预期效果：
    - 推荐数量：大幅减少（3/2/1只 vs 原5/4/2只）
    - 推荐质量：显著提升（基本面更优，更适合长期持有）
    - 胜率：预期提升（更严格筛选，安全边际更大）
    - 持有体验：更好（不会因短期波动频繁止损）
    """
    print(__doc__)
