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
import logging
import numpy as np
import pandas as pd
from typing import Optional

logger = logging.getLogger("longterm")


@dataclass
class LongTermQualityConfig(StrategyConfig):
    """长期投资版配置：继承原策略配置，覆盖关键参数

    2026-10-13 修复（此前该类**无法构造**，run_longterm.py 必然崩溃）：
      1. RECOMMENDATION_MODE 原为 "quality_longterm"，而基类 __post_init__ 的
         _validate_recommendation_config 只接受 {"quality_value","technical"}，
         构造时直接抛 ValueError。该策略走的就是 quality_value 荐股资格
         （evaluate_quality_longterm 内部委派 evaluate_quality_value），
         故正确取值就是 "quality_value"；对外展示名由 LONGTERM_LABEL 承载。
      2. QV_RECENT_LOW_MAX_AGE 原为 90、QV_RECENT_LOW_WINDOW 也为 90，
         基类校验要求 min <= max < window（严格小于），90 < 90 为假 → ValueError。
         底部回看窗口放宽到 120，仍保证 max_age(90) < window(120)。
    """
    
    # 对外展示名（飞书卡片 / 落库 strategy / 日志），与 RECOMMENDATION_MODE 解耦：
    # RECOMMENDATION_MODE 决定**走哪条荐股资格**，展示名决定**怎么称呼这条口径**。
    LONGTERM_LABEL: str = "长期优质低估低位"
    
    # ========== 1. 更严格的质量要求 ==========
    # quality_value 路径（本策略与生产同源），不是第三种资格模式
    RECOMMENDATION_MODE: str = "quality_value"
    
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
    
    # 止跌确认模式。默认保持基类 strict（生产同源，止跌确认是硬否决）。
    # 早期版本强制 risk_only，但按「宁缺毋滥」原则复核后**撤回**：
    # risk_only 把「站上 MA20」从否决降级为标签，等于放行更多票 = 更宽松，
    # 与项目最高优先级原则冲突；基类实测也表明 strict 保留组前瞻收益更优。
    # 如需 A/B 对照可显式改为 "risk_only"（基类仍完整支持该分支）。
    QV_STABILIZATION_MODE: str = "strict"
    
    # MACD深度弱势阈值（仅在 QV_STABILIZATION_MODE="risk_only" 或显式开启
    # QV_ENFORCE_KDJ_MACD_VETO 时生效；strict 模式下不参与判定）
    MACD_WEAK_DAYS: int = 3  # 连续3日恶化（vs 原2日）
    MACD_WEAK_HIST_PCT: float = -0.8  # 深度弱势阈值从-0.5%收紧至-0.8%
    
    # 下降通道加速检测：MA20 近 5 日跌幅超过阈值视为「加速下跌」。
    # 2026-10-13：**已接线**（此前声明但从未被任何代码读取，是典型的死配置——
    # 死配置比没有配置更危险，它制造「已有加速下跌防护」的虚假安全感）。
    # 实现见 _detect_accelerating_decline()，判定为**否决**项（只做减法）。
    DETECT_ACCELERATING_DECLINE: bool = True
    MA20_ACCELERATE_THRESHOLD: float = -0.05  # MA20 5日跌幅阈值
    MA20_ACCELERATE_LOOKBACK: int = 5  # 计算 MA20 斜率的回看交易日数
    
    # 成交量萎缩判定（近5日均量 < 近20日均量的该比例 → 视为缩量见底信号）。
    # 2026-10-13：**已接线**，且只作为**加分标签**（不改准入、不参与排序），
    # 定位是给人工裁量的补充信息，而非风控项——风控项若长期 0 命中即为死闸门。
    DETECT_VOLUME_EXHAUSTION: bool = True
    VOLUME_EXHAUSTION_RATIO: float = 0.5
    
    # ========== 3. 更宽松的底部判定窗口 ==========
    # QV_RECENT_LOW_WINDOW 必须 > QV_RECENT_LOW_MAX_AGE（基类硬校验）
    QV_RECENT_LOW_WINDOW: int = 120  # 底部回看窗口（> max_age）
    QV_RECENT_LOW_MIN_AGE: int = 3  # 最小年龄从5天降至3天（允许快速反弹）
    QV_RECENT_LOW_MAX_AGE: int = 90  # 最大年龄从40天扩展至90天（允许长期磨底）
    
    # 距底涨幅上限。2026-10-13：删除原 QV_LOW_GAIN_MIN_PCT/QV_LOW_GAIN_MAX_PCT
    # ——它们从未被读取（实际口径由 QV_LOW_GAIN_ATR_MULT 经 ATR 自适应给出，
    # 并被 np.clip 夹在 [10%, 20%]）。保留两个永不生效的数字只会让人
    # 以为「距底涨幅」有 15%~35% 的独立区间约束。
    QV_LOW_GAIN_ATR_MULT: float = 8.0
    
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
    # 2026-10-13：删除 ALLOW_PB_FALLBACK / PB_FALLBACK_THRESHOLD / PB_FALLBACK_PE_MAX。
    # 原因：PB 早已不是 quality_value 的准入项（偏高只打「PB偏高」风险标签），
    # 基类 evaluate_quality_value 中**没有任何代码读取**这三个字段——
    # 即「PE超标但PB<1.0可豁免」这条放宽规则从未生效、也无法生效。
    # 且它本身是**放宽**方向（放行更多票），与「宁缺毋滥」原则直接冲突。
    # 死配置 + 放宽意图 = 双重风险，故删除而非接线。
    
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
    
    # 长期投资更重视质量和估值，降低技术权重。
    # 权重和 = 1.0（基类 __post_init__ 会硬校验，失衡直接抛错）。
    QUALITY_SCORE_WEIGHT: float = 0.55
    VALUATION_SCORE_WEIGHT: float = 0.35
    TECHNICAL_SCORE_WEIGHT: float = 0.10
    
    # 最低质量分要求（硬门槛）。
    # 2026-10-13：由 65 回调到 60。理由：65 与年度资格闸门
    # （中位ROE≥12% + 最低ROE≥8% + 现金转换≥0.9）重复计权，
    # 实测会把「年度质量已 verified」的绝大多数候选二次淘汰，
    # 使长期策略几乎恒定 0 推荐。60 仍高于基类默认 0，是真实的额外收紧。
    MIN_QUALITY_SCORE: float = 60.0
    
    # 技术分门槛：技术面在本策略中只做「不追高」的最低约束，
    # 保持基类 40 分口径，不下调——下调会让「技术证据不足」的票混入正式推荐。
    MIN_TECHNICAL_SCORE_FORMAL: float = 40.0
    
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
            "total_years": int,            # 可比较的同比次数 = 实际年报数 - 1
            "consistency_score": 0-100
        }

    2026-10-13 修复（此前该闸门对**每一只**股票都返回 insufficient）：
    原实现要求 `len(annual_rows) >= min_years + 1`（要算 N 次同比得�� N+1 条年报），
    但上游 `enrich_annual_fundamentals` 只按 `config.QUALITY_YEARS`(=5) 取 5 条，
    于是 5 < 6 恒成立 → 每只股票都 FAIL_EARNINGS_DATA_INSUFFICIENT →
    长期策略**永远 0 推荐**，且日志里只有一个看不出原因的统一否决码。

    修正为「N 条年报 → N-1 次同比」：这是「连续 N 个完整年度」这一自然口径下
    能得到的全部同比次数，不再要求一条取不到的额外年报。
    注意 `MIN_POSITIVE_GROWTH_YEARS` 的含义随之从「N 次里 M 次」变为
    「N-1 次里 M 次」（5 条年报 = 4 次同比，至少 3 次为正 = 75%），
    比例门槛不降反升，符合「宁缺毋滥」。
    """
    if not annual_rows or len(annual_rows) < min_years:
        return {"status": "insufficient", "positive_growth_years": 0,
                "total_years": 0, "consistency_score": 0}

    window = annual_rows[-min_years:]
    if len(window) < 2:
        return {"status": "insufficient", "positive_growth_years": 0,
                "total_years": 0, "consistency_score": 0}

    growth_years = 0
    total_years = 0

    for prev_row, curr_row in zip(window, window[1:]):
        curr_profit = curr_row.get("net_profit")
        prev_profit = prev_row.get("net_profit")

        if curr_profit is None or prev_profit is None:
            return {"status": "insufficient", "positive_growth_years": 0,
                    "total_years": 0, "consistency_score": 0}

        if not np.isfinite(curr_profit) or not np.isfinite(prev_profit):
            return {"status": "insufficient", "positive_growth_years": 0,
                    "total_years": 0, "consistency_score": 0}

        # 去年同期为负/零时同比增长率无意义，不计入正增长（也不当作失败），
        # 否则「去年亏损今年扭亏」这种明确的改善会被误判为不持续。
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


def _detect_accelerating_decline(technical: pd.DataFrame, config: LongTermQualityConfig) -> dict:
    """下降通道加速检测：MA20 在近 N 个交易日的跌幅超过阈值 → 加速下跌。

    这是长期策略**唯一新增的技术面否决项**，对应「左侧低吸但绝不接加速下跌的刀」
    这一原始设计意图。此前 DETECT_ACCELERATING_DECLINE / MA20_ACCELERATE_THRESHOLD
    只是配置里的两个数字，**没有任何代码读取它们**——死配置比没有配置更危险，
    它制造「已有加速下跌防护」的虚假安全感（与项目「不可观测的风控 = 没有风控」原则冲突）。

    语义严格取「否决」而非「标签」：数据缺失一律**不否决**（missing 不误杀，
    由上层 FAIL_DATA / 停牌缺口闸门负责），只有确凿的加速下跌才拦。
    """
    result = {"checked": False, "accelerating": False, "ma20_slope": None,
              "threshold": float(getattr(config, "MA20_ACCELERATE_THRESHOLD", -0.05)),
              "lookback": int(getattr(config, "MA20_ACCELERATE_LOOKBACK", 5))}
    if technical is None or not isinstance(technical, pd.DataFrame) or technical.empty:
        return result
    if not {"ma20", "close"}.issubset(technical.columns):
        return result
    n = max(1, result["lookback"])
    if len(technical) < n + 1:
        return result
    ma = pd.to_numeric(technical["ma20"], errors="coerce")
    now, prev = ma.iloc[-1], ma.iloc[-(n + 1)]
    if pd.isna(now) or pd.isna(prev) or not np.isfinite(now) or not np.isfinite(prev) or prev <= 0:
        return result
    slope = float((now - prev) / prev)
    result.update(checked=True, ma20_slope=round(slope, 5),
                  accelerating=slope < result["threshold"])
    return result


def _detect_volume_exhaustion(technical: pd.DataFrame, config: LongTermQualityConfig) -> dict:
    """缩量见底检测：近 5 日均量 < 近 20 日均量的 VOLUME_EXHAUSTION_RATIO。

    **只作展示标签，不参与准入、不参与排序**。理由：它是一个「形态证据」而非
    「风险闸门」——把形态证据做成硬闸门，在没有前瞻实测支撑时等于凭空收紧，
    且该类闸门极易退化成长期 0 命中的死闸门。给人看、让人裁量，才是它的正确位置。
    """
    out = {"checked": False, "exhausted": False, "ratio": None,
           "threshold": float(getattr(config, "VOLUME_EXHAUSTION_RATIO", 0.5))}
    if technical is None or not isinstance(technical, pd.DataFrame) or technical.empty:
        return out
    if "volume" not in technical.columns:
        return out
    vol = pd.to_numeric(technical["volume"], errors="coerce").dropna()
    if len(vol) < 20:
        return out
    recent = float(vol.tail(5).mean())
    base = float(vol.tail(20).mean())
    if not np.isfinite(recent) or not np.isfinite(base) or base <= 0:
        return out
    ratio = recent / base
    out.update(checked=True, ratio=round(ratio, 4), exhausted=ratio < out["threshold"])
    return out


def evaluate_quality_longterm(daily_df: Optional[pd.DataFrame], code: str, name: str,
                              config: LongTermQualityConfig, market_env: Optional[dict] = None,
                              fund_data: Optional[dict] = None,
                              latest_trade_date: Optional[str] = None,
                              val_context: Optional[dict] = None,
                              index_df: Optional[pd.DataFrame] = None,
                              volatile_out: Optional[list] = None):
    """长期投资版评估函数（在 evaluate_quality_value 基础上增强）

    长期专项增强（相对生产 quality_value 的**收紧**，只做减法）：
    1. 5年质量要求（vs 原3年）
    2. ROE稳定性检查（CV 上限）
    3. 盈利持续性检查（同比正增长年数）
    4. 长期现金流质量检查（5年累计 OCF/净利润 ≥ 1.0）
    5. 长期投资资质综合评分门槛
    6. 相对强度硬门槛（不严重跑输市场）
    7. 下降通道加速下跌否决（新增，接线自原死配置）
    8. 缩量见底展示标签（新增，仅展示）

    2026-10-13 修复（此前该函数**对每一只股票都返回 FAIL_*，长期策略恒 0 推荐**）：
    `_screen_quality_pool` 采用「两趟评估」——第一趟传 `fund_data=None` 做廉价
    行情预筛，通过后才取财务数据做第二趟。而本函数在**第一趟**就执行了
    ROE/现金流/盈利持续性三项年度财务闸门，此时 `annual_rows == []` →
    三项全部 insufficient → 每只股票在预筛阶段即被否决，财务数据**永远取不到**。
    修复：年度财务类闸门只在 `fund_data` 非空（即第二趟）时执行；第一趟
    仅执行不依赖财务数据的技术面否决（加速下跌）。这也符合上游预筛的设计意图
    ——「先廉价行情筛选，后年度财务核验」。
    """
    from src.bottom_fishing_strategy import evaluate_quality_value

    # 先调用原有评估逻辑（quality_value 全部闸门：流动性/低位/估值/止跌/ATR…）
    signal, reason = evaluate_quality_value(
        daily_df, code, name, config, market_env, fund_data,
        latest_trade_date, val_context, index_df, volatile_out
    )

    # 原评估已经淘汰，直接返回
    if signal is None:
        return None, reason

    fund = fund_data or {}
    annual_rows = fund.get("annual_rows", [])

    # ===== 技术面否决：下降通道加速下跌（不依赖财务数据，两趟都执行）=====
    if getattr(config, "DETECT_ACCELERATING_DECLINE", False):
        technical = None
        try:
            from src.bottom_fishing_strategy import compute_daily_signals
            if daily_df is not None and not daily_df.empty:
                technical = compute_daily_signals(daily_df.copy(), config)
        except Exception as exc:  # noqa: BLE001 形态指标失败不得误杀
            logger.debug("%s(%s) 加速下跌检测异常（放行）: %s", name, code, exc)
        accel = _detect_accelerating_decline(technical, config)
        if accel["accelerating"]:
            logger.info("[LT_ACCEL] %s(%s) MA20 近%d日 %.2f%% < 阈值 %.2f%%，判定加速下跌",
                        name, code, accel["lookback"], accel["ma20_slope"] * 100,
                        accel["threshold"] * 100)
            return None, "FAIL_ACCELERATING_DECLINE"

    # ===== 年度财务类闸门：仅在拿到财务数据时执行（第二趟）=====
    # 第一趟（fund_data is None）刻意跳过：预筛阶段的职责是廉价行情筛选，
    # 此时年度财务尚未取回，判定必然是「数据不足」，只会造成全池误杀。
    if fund_data is None:
        return signal, reason

    # 1. ROE稳定性检查
    if getattr(config, "REQUIRE_ROE_STABILITY", False):
        roe_stab = evaluate_roe_stability(annual_rows, config.QUALITY_YEARS)
        if roe_stab["status"] == "unstable":
            return None, "FAIL_ROE_UNSTABLE"
        if roe_stab["status"] != "verified":
            return None, "FAIL_ROE_STABILITY_INSUFFICIENT"
        if float(roe_stab["roe_cv"]) > float(config.MAX_ROE_CV):
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
        if cash_quality["status"] == "poor":
            return None, "FAIL_LONGTERM_CASH_POOR"
        if cash_quality["status"] != "verified":
            return None, "FAIL_LONGTERM_CASH_INSUFFICIENT"
        if float(cash_quality["cash_ratio"]) < float(longterm_cash_min):
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
    #    数据不可计算（rs is None）时**不否决**：相对强度本就只是排序微调，
    #    升级为硬门槛后，指数数据缺失会连带把整池打成 0 推荐。
    if getattr(config, "REQUIRE_RELATIVE_STRENGTH", False):
        rs = signal.relative_strength
        rs_min = getattr(config, "RS_MIN_THRESHOLD", -15.0)
        if rs is not None and float(rs) < float(rs_min):
            return None, "FAIL_RELATIVE_STRENGTH_WEAK"

    # 6. 周期股深度低估标签（仅展示，不放宽任何准入）
    from src.recommendation_factors import classify_cyclical_industry
    industry_name = str((val_context or {}).get("industry", ""))
    cycle_class = classify_cyclical_industry(
        industry_name, config.CYCLICAL_INDUSTRY_CODES,
        config.CYCLICAL_INDUSTRY_KEYWORDS)

    extra_tags: list[str] = []
    if cycle_class == "cyclical" and getattr(config, "CYCLICAL_PB_FALLBACK_ENABLED", False):
        pb = signal.pb_mrq
        pb_floor = getattr(config, "CYCLICAL_PB_FLOOR", 0.8)
        if pb is not None and np.isfinite(pb) and 0 < float(pb) < float(pb_floor):
            # 仅打标签说明「周期底部深度低估」，**不放宽 PE/PB 任何准入**。
            # 原注释「给PE更多容忍度」与实际行为（只加标签）不符，已更正。
            extra_tags.append(f"周期底部深度低估PB={float(pb):.2f}")

    # 7. 缩量见底展示标签（不参与准入/排序）
    if getattr(config, "DETECT_VOLUME_EXHAUSTION", False):
        try:
            from src.bottom_fishing_strategy import compute_daily_signals
            if daily_df is not None and not daily_df.empty:
                tech_for_vol = compute_daily_signals(daily_df.copy(), config)
                vol = _detect_volume_exhaustion(tech_for_vol, config)
                if vol["exhausted"]:
                    extra_tags.append(f"缩量见底(5日/20日量比{vol['ratio']:.2f})")
        except Exception as exc:  # noqa: BLE001 形态指标失败不得影响主流程
            logger.debug("%s(%s) 缩量检测异常（忽略）: %s", name, code, exc)

    # 8. 更新信号对象的长期投资相关字段
    tags = signal.signals_hit.split(",")
    if tags:
        tags[0] = config.LONGTERM_LABEL  # 主标签改为长期口径名

    if ltq_eval["status"] != "disabled":
        tags.append(f"长期投资资质{ltq_eval['ltq_score']:.0f}分")
        bd = ltq_eval["breakdown"]
        if bd["roe_stability"]["score"] >= 80:
            tags.append("ROE高度稳定")
        if bd["cash_quality"]["score"] >= 80:
            tags.append("现金流优质")
        if bd["earnings_consistency"]["score"] >= 80:
            tags.append("盈利持续增长")
    tags.extend(extra_tags)

    signal.signals_hit = ",".join(t for t in tags if t)

    # 持有期提示：写入 Signal 的正式字段（此前写的是不存在的属性，
    # 既不落库也不进卡片，等于没写），供 describe_trade_plan 渲染 30~90 天口径。
    signal.holding_days_hint_min = int(config.HOLD_DAYS_HINT_MIN)
    signal.holding_days_hint_max = int(config.HOLD_DAYS_HINT_MAX)

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
