"""
飞书 Webhook 通知模块

通过飞书自定义机器人 Webhook 发送选股结果、周度追踪报告、信号归因月报。
环境变量 FEISHU_WEBHOOK_URL 未配置时静默跳过。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger("feishu")

WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def _send_feishu(card: dict) -> bool:
    """发送飞书消息卡片，返回是否成功。"""
    if not WEBHOOK_URL:
        logger.info("FEISHU_WEBHOOK_URL 未配置，跳过飞书通知")
        return False
    try:
        payload = {"msg_type": "interactive", "card": card}
        resp = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            logger.info("飞书通知发送成功")
            return True
        else:
            logger.warning("飞书通知返回异常: %s", data)
            return False
    except Exception as e:
        logger.warning("飞书通知发送失败: %s", e)
        return False


def _build_header(title: str, color: str = "blue") -> dict:
    """构建消息卡片头部。"""
    return {
        "title": {"tag": "plain_text", "content": title},
        "template": color,
    }


def _md_element(content: str) -> dict:
    """构建 markdown 元素。"""
    return {"tag": "markdown", "content": content}


def _divider() -> dict:
    return {"tag": "hr"}


def _stock_card(header_title: str, body_md: str, color: str = "green") -> dict:
    """构建单只个股的独立卡片。

    拆卡目的：飞书群里一张长卡片难以聚焦，多只股票塞在一起时"操作计划"等
    关键信息被埋在滚动条深处。改为每股一张卡后，用户在群里就能一眼扫过
    标题（含策略口径 + 序号 + 名称代码），点开即看该股的完整评估与操作计划。
    body_md 由调用方组装（describe/describe_breakout + 口径脚注），
    保证单张卡即便脱离上下文也可独立阅读。
    """
    return {
        "header": _build_header(header_title, color=color),
        "elements": [_md_element(body_md)],
    }


_STRATEGY_ZH = {"bottom_fishing": "优质低估低位", "volume_breakout": "放量突破"}
# 卡片标题前缀 = 该次运行**实际生效的资格口径名**，而不是落库用的 strategy 值。
# 原因：DB 的 strategy='bottom_fishing' 是一个覆盖两种口径的 sleeve 标识——
#   · quality_value（生产默认）：多年质量 + 行业相对低估 + 250 日低位，**不是技术抄底**；
#   · technical（对照/旧规则）：RSI 超卖 + 底背离 + 回撤深度，才是真正的技术抄底。
# 此前统一显示为【抄底策略】，会把前者（绝大多数）误标成后者，故按实际口径取名。
# 例：【优质低估低位】选股结果 / 【放量突破】选股结果 / 【低位企稳】选股结果（technical）
_LOW_POSITION_ROLE = "低位企稳"
_STRATEGY_ROLE_ZH = {"bottom_fishing": "优质低估低位", "volume_breakout": "放量突破"}
_VOLATILE_STRATEGY_LABEL = {"bottom_fishing": "优质低估低位信号", "volume_breakout": "放量突破信号"}
VOLATILE_CARD_TOP = 5   # 卡片内逐只展示上限（超出仅提示条数，避免卡片过长）
# 飞书通知每策略最多展示的股票数。策略层已按 MAX_PICKS/NEUTRAL_MAX_PICKS/BEAR_MAX_PICKS
# 截取正式推荐，这里再做一次通知口径的收敛：宁缺毋滥，用户只看 Top-3。
NOTIFY_TOP_PER_STRATEGY = 3

# ===== 估值口径声明 =====
# 行业相对估值依赖单一 Baostock 接口 query_stock_industry；它不可用时全市场回退绝对阈值
# （PE≤25 / PB≤3）——而绝对阈值正是本项目刻意避开的「名单压向银行/地产/周期」口径。
# 回退是合法降级，但不能让用户在一份「看起来正常」的名单上误判口径，故在卡片显式声明。
_VALUATION_MODE_NOTE = {
    "absolute": (
        "\n⚠️ **估值口径：本次为绝对阈值回退**（行业相对估值未生效——行业分类/行业快照不可用，"
        "或行业内可比样本不足）。名单会偏向低 PE/PB 的银行、地产、周期板块，"
        "**请勿与「行业相对低估」口径的结果直接比较**。"
    ),
    "mixed": (
        "\n⚠️ **估值口径：本次为混合口径**——部分标的因行业样本不足回退到绝对阈值"
        "（PE≤25 / PB≤3），同一份名单内的「便宜」含义不完全可比。"
    ),
}

# ===== 名单口径与操作说明 =====
# 「候选」措辞已废弃：本函数在渲染前就过滤掉了全部非 formal 标的（见函数开头的
# tier 复核），因此卡片上出现的每一只都是**正式推荐**——已落库、并被周度追踪统计战绩。
# 原先写成「XX候选」会被读成「备选/仅供参考」，与 formal 的真实含义相反。
# 现统一为「正式推荐 · XX口径」，并在每只票下渲染「操作计划」块（建仓区间 /
# 止损 / 止盈 / 建议持有周期，见 bottom_fishing_strategy.describe_trade_plan）。
_RECOMMENDATION_NOTE = (
    "\n**名单口径:** 下列均为**正式推荐**（已通过全部筛选闸门、已落库并纳入周度追踪）；"
    "\n每只票下方的**操作计划**为计划价位，按**次日开盘**执行，仓位与资金管理请自行判断。"
)


def _volatile_elements(volatile: Optional[pd.DataFrame], strategy: str = "bottom_fishing",
                       top: int = VOLATILE_CARD_TOP) -> list:
    """构建「波动率风控否决」区块（高风险观察池）。

    这些标的不是数据缺失、也不是形态不合格，而是**波动太大**：ATR 占现价百分比
    超出风控上限，按 1.5×ATR 设止损需先承受较深浮亏才确认失败，日内噪声即可能扫损。
    单独成区块并显式标注风险等级与风险提示——它们是「今天为什么没推荐」的直接答案，
    也是可以持续观察（ATR% 收敛后可能重新达标）的对象，但绝不是买入建议。
    """
    if volatile is None or volatile.empty:
        return []
    try:
        from bottom_fishing_strategy import describe_volatile, sort_volatile
    except ImportError:
        describe_volatile = sort_volatile = None

    # 并发筛选的完成顺序不确定，这里按「技术分降序 → ATR% 降序」统一排序，
    # 保证卡片里的 Top-N 与日志 [VOLATILE] 区块逐只对应、两次运行结果可复现。
    rows = volatile.to_dict("records")
    if sort_volatile:
        rows = sort_volatile(rows)
    total = len(rows)
    top_n = max(1, int(top))

    label = _VOLATILE_STRATEGY_LABEL.get(strategy, "信号")
    elems = [
        _divider(),
        _md_element(
            f"**⚠️ 波动率风控否决 {total} 只**"
            f"（{label}已达标，仅因 ATR 超风控上限被拦下，未进入正式推荐）\n"
            f"以下为**高风险观察池**，ATR% 收敛至上限以内后才可能重新达标；"
            f"列出仅供观察与复核，**不构成买入建议**。"
        ),
    ]
    for r in rows[:top_n]:
        if describe_volatile:
            elems.append(_md_element(describe_volatile(r)))
        else:
            elems.append(_md_element(
                f"**{r.get('name', '')} {r.get('code', '')}**"
                f" | 评分: {r.get('score', 0)} ({r.get('grade', '-')}级)"
                f" | ATR: {r.get('atr_pct', '-')}% / 上限 {r.get('atr_limit', '-')}%"
                f" | 风险: {r.get('risk_level', '-')}"
            ))
    if total > top_n:
        elems.append(_md_element(f"*...共 {total} 只，仅展示前 {top_n} 只*"))
    return elems


def notify_screening_result(
    df: Optional[pd.DataFrame],
    market_env: str = "unknown",
    error_msg: Optional[str] = None,
    pending: Optional[pd.DataFrame] = None,
    strategy: str = "bottom_fishing",
    volatile: Optional[pd.DataFrame] = None,
    data_degraded: bool = False,
    valuation_mode: Optional[str] = None,
    recommendation_mode: str = "quality_value",
) -> None:
    """发送选股结果通知。

    参数匹配 run.py / run_breakout.py 中的调用:
        notify_screening_result(output_df, market_env=market_env_desc, strategy="bottom_fishing")
        notify_screening_result(output_df, market_env=market_env_desc, strategy="volume_breakout")
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))

    展示口径：df 为正式推荐，每策略最多 NOTIFY_TOP_PER_STRATEGY=3 只（宁缺毋滥）；
    volatile 为「波动率风控否决」的高风险观察池（技术面/形态已达标，仅 ATR 超限被拦），
    单独成区块并显式标注风险等级与风险提示，避免被误读为推荐标的。

    **卡片形态（2026-09 改造）**：由"一张长卡片塞下所有股票"改为"汇总卡 + 每股独立卡"：
      · 1 张汇总卡：时间 / 市场环境 / 推荐数量 / 名单口径 / 数据源与估值口径提示；
      · N 张个股卡：每张标题带 [i/N] 序号 + 名称代码，卡内含 describe 全文 + 口径脚注
        （策略口径 / 市场环境 / 时间 / 序号），保证单张卡在群里脱离汇总也可独立阅读；
      · 1 张波动率观察池卡（若存在）：仍是单卡多行——每只是一行简述，拆开反而碎片化。
    拆分带来的额外收益：飞书群里点击通知栏预览即可看到"哪只票"，不用先展开长卡片；
    单张卡片高度可控，操作计划不再被埋在滚动条深处。
    strategy 决定卡片标题前缀与单票描述格式（优质低估低位口径=「正式推荐 · 优质低估低位」/
    突破入口=「正式推荐 · 放量突破」/ technical 对照口径=「正式推荐 · 低位企稳」），
    每只票另附「操作计划」块（建仓区间 / 止损 / 止盈 / 建议持有周期）。
    两策略通知的卡片标题以「实际生效口径名」前缀区分：【优质低估低位】（quality_value，
    生产默认）/【放量突破】（technical 的突破入口）；technical 的抄底对照口径显示为
    【低位企稳】。用口径名而非 strategy 值，是为了让标题说出真实的选股逻辑（见 _STRATEGY_ROLE_ZH）。

    data_degraded：主源 Baostock 熔断、全程走 AkShare。此时逐 bar 估值字段与成长数据
    都拿不到，quality_value 的估值闸门与前瞻确认会双双记缺项 → formal 常为 0。
    **这种零推荐是数据问题而非市场问题**，故降级日的卡片标题直接写明，避免误读。

    valuation_mode：本次实际生效的估值口径（"industry" / "absolute" / "mixed"），
    由 strategy 层写入 output_df 的 valuation_mode 列后透传。为 "absolute"/"mixed" 时
    卡片会显式声明「已回退绝对阈值」——绝对口径会系统性偏向低 PE/PB 的传统板块。

    recommendation_mode：本次实际生效的资格判定模式（quality_value / technical），
    决定标题里的口径名。必须由调用方显式给出：零推荐时没有任何行可供推断口径，
    只凭 df 会把 production 的 quality_value 误标为【低位企稳】（technical 对照口径）。

    注：pending（待核验候选）参数保留以兼容旧调用签名，但**不再在飞书卡片中渲染**——
    待核验意味着数据不全、既不构成推荐也不该被误读为备选，仅在 CI 日志中打印计数即可。
    """
    # 通知边界再次核验资格，兼容调用方误传候选或缺少 tier 的旧数据。
    if df is not None and not df.empty:
        formal = df["tier"].eq("formal") if "tier" in df.columns else pd.Series(False, index=df.index)
        omitted = len(df) - int(formal.sum())
        if omitted:
            logger.warning("通知过滤 %d 只非 formal 标的（含核验状态缺失）", omitted)
        df = df.loc[formal].copy()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_breakout = strategy == "volume_breakout"
    candidate_label = "放量突破" if is_breakout else "低位企稳"
    # quality_mode：本次跑的是 quality_value 资格（周线确认 not_required 是其标志），
    # 与 technical（真正的技术抄底）区分开——两者的入选逻辑完全不同，标题不应混用。
    # 注意：**零推荐时从数据里推不出口径**（没有任何行可供判断），所以标题不能只依赖它，
    # 必须由调用方显式传 recommendation_mode（run.py 传 config.RECOMMENDATION_MODE）。
    quality_mode = any(frame is not None and not frame.empty and "weekly_status" in frame
                       and frame["weekly_status"].eq("not_required").any()
                       for frame in (df, pending))
    if quality_mode:
        candidate_label = "优质低估低位"
    # 标题即「实际口径名 + 选股结果」，不再出现【抄底策略】优质低估低位荐股 这类自相矛盾的组合。
    if is_breakout:
        _role = _STRATEGY_ROLE_ZH["volume_breakout"]
    elif str(recommendation_mode or "").strip().lower() == "technical":
        _role = _LOW_POSITION_ROLE
    else:
        _role = _STRATEGY_ROLE_ZH["bottom_fishing"]
    card_title = f"【{_role}】选股结果"

    # 异常通知
    if error_msg:
        card = {
            "header": _build_header(f"{card_title}执行异常", color="red"),
            "elements": [
                _md_element(f"**时间:** {now}\n**市场环境:** {market_env}"),
                _divider(),
                _md_element(f"**错误信息:**\n```\n{error_msg[:500]}\n```"),
            ],
        }
        _send_feishu(card)
        return

    # 尝试导入 describe 函数（按策略来源选择卡片格式）
    try:
        from src.bottom_fishing_strategy import describe, describe_volatile
    except ImportError:
        describe = None
        describe_volatile = None
    try:
        from src.volume_breakout_strategy import describe_breakout
    except ImportError:
        describe_breakout = None
    describe_fn = (describe_breakout or describe) if is_breakout else describe

    # 无信号
    if df is None or df.empty:
        if data_degraded:
            # 降级日的零推荐首先是**数据问题**，标题就要讲明白，不能指望用户去读正文——
            # 否则「今天没有好票」与「今天没有数据」在第一时间无法区分。
            no_signal_text = (
                "**本次不足以产生正式推荐：原因是数据源，不是市场。**\n\n"
                "Baostock 不可用，已回退 AkShare：AkShare 逐 bar 不提供估值字段（peTTM/pbMRQ），"
                "也没有成长数据通道，优质低估低位的**估值闸门与前瞻确认会双双记缺项**，"
                "候选整体降级为待核验，formal 正式推荐因此为 0。"
                "**这不等于「今天没有合格标的」**，请先复核数据源（是否被限流/熔断）后重跑。"
            )
        else:
            no_signal_text = "今日无正式推荐（未发现数据完整且通过全部条件的标的），宁可少荐。"
        if volatile is not None and not volatile.empty:
            no_signal_text += (f"\n本轮有 {len(volatile)} 只仅因**波动率超限**被拦下"
                               f"（技术面已达标），详见下方高风险观察池。")
        no_signal_text += _VALUATION_MODE_NOTE.get(str(valuation_mode), "")
        card = {
            "header": _build_header(
                (f"{card_title} - 数据源不足，本次无正式推荐" if data_degraded
                 else f"{card_title} - 今日无信号")
                + (f"（波动率观察池 {len(volatile)} 只）" if volatile is not None and not volatile.empty else ""),
                color="orange" if data_degraded else "grey"),
            "elements": [
                _md_element(f"**时间:** {now}\n**市场环境:** {market_env}\n\n{no_signal_text}"),
                *_volatile_elements(volatile, strategy),
            ],
        }
        _send_feishu(card)
        return

    # 有信号：每策略最多展示 NOTIFY_TOP_PER_STRATEGY 只
    # 展示形态：**汇总卡 1 张 + 每只个股独立卡 N 张 + 波动率观察池 1 张**
    #   · 汇总卡承载市场环境、名单口径、数据源/估值口径提示——全局只讲一次；
    #   · 每只个股独立成卡，标题带 [i/N] 序号 + 名称代码便于群内快速扫读；
    #     卡内自带"口径脚注"（策略口径 + 市场环境 + 时间），保证脱离汇总卡也可独立阅读；
    #   · 波动率观察池保持单卡（每只是一行简述，拆开反而碎片化）。
    top_n = max(1, int(NOTIFY_TOP_PER_STRATEGY))
    shown_df = df.head(top_n)
    shown_n = len(shown_df)
    hit_note = (f"，本策略命中 {len(df)} 只，展示 Top-{top_n}" if len(df) > shown_n else "")
    degraded_note = (
        "\n⚠️ **数据源已降级（AkShare 兜底）**：估值字段与成长数据可能缺失，"
        "名单可能不完整（部分标的已被降级为待核验），请留意每只的核验状态。"
        if data_degraded else ""
    )
    valuation_note = _VALUATION_MODE_NOTE.get(str(valuation_mode), "")
    volatile_n = int(len(volatile)) if volatile is not None else 0

    # ---------- 1) 汇总卡 ----------
    summary_text = (
        f"**时间:** {now}\n"
        f"**市场环境:** {market_env}\n"
        f"**推荐数量:** {shown_n} 只（均为**正式推荐** · {candidate_label}{hit_note}）"
        + _RECOMMENDATION_NOTE
        + degraded_note
        + valuation_note
        + f"\n\n📇 下方将**逐只发送 {shown_n} 张个股卡片**（含操作计划）"
        + (f"，另附**波动率观察池 {volatile_n} 只**（单独 1 张卡）。" if volatile_n else "。")
    )
    _send_feishu({
        "header": _build_header(f"{card_title} - {shown_n}只信号", color="green"),
        "elements": [_md_element(summary_text)],
    })

    # ---------- 2) 每只个股独立成卡 ----------
    # 卡内脚注复述"策略口径 / 市场环境 / 时间"，让每张卡在群里独立可读——
    # 用户从通知栏点进某一张时不必回滚找汇总卡。
    per_stock_footer_tmpl = (
        "\n\n---\n*口径: 正式推荐 · {label} | 市场环境: {env} | {ts} | [{idx}/{total}]*"
    )
    for idx, (_, row) in enumerate(shown_df.iterrows(), start=1):
        r = row.to_dict()
        if describe_fn:
            body = describe_fn(r)
        else:
            body = (
                f"**{r.get('name', '')} {r.get('code', '')}**\n"
                f"评分: {r.get('score', 0)} ({r.get('grade', '')}) "
                f"| 收盘: {r.get('close', 0)} "
                f"| 止损: {r.get('stop_loss', 0)} "
                f"| 止盈: {r.get('take_profit', 0)} "
                f"| RR: {r.get('rr_ratio', 0)}"
            )
        body += per_stock_footer_tmpl.format(
            label=candidate_label, env=market_env, ts=now, idx=idx, total=shown_n,
        )
        name = r.get('name', '') or '-'
        code = r.get('code', '') or '-'
        _send_feishu(_stock_card(
            f"{card_title} [{idx}/{shown_n}] {name} {code}",
            body,
            color="green",
        ))

    # ---------- 3) 波动率观察池（单卡汇总） ----------
    if volatile_n:
        vol_elements = _volatile_elements(volatile, strategy)
        # _volatile_elements 首个元素是分隔线，独立成卡时无需（卡片头已有视觉分隔）
        if vol_elements and vol_elements[0].get("tag") == "hr":
            vol_elements = vol_elements[1:]
        _send_feishu({
            "header": _build_header(
                f"{card_title} - 波动率观察池 {volatile_n}只（不构成买入建议）",
                color="orange",
            ),
            "elements": vol_elements,
        })


def notify_tracking_result(report: Optional[pd.DataFrame]) -> None:
    """发送周度追踪报告。

    参数匹配 run_weekly_tracking.py 中的调用:
        notify_tracking_result(report)

    report 含 strategy 列时按策略来源分列统计（优质低估低位/放量突破各自的数量/胜率/平均收益），
    逐只明细行也带 [优质低估低位]/[放量突破] 前缀标签。
    """
    if report is None or report.empty:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(report)
    win = len(report[report["return_pct"] > 0])
    win_rate = win / total * 100 if total > 0 else 0
    avg_return = report["return_pct"].mean()

    # 状态分布
    status_counts = report["status"].value_counts().to_dict()
    status_text = " | ".join([f"{k}: {v}只" for k, v in status_counts.items()])

    # 按策略来源分列（无 strategy 列的旧调用保持单段汇总）
    strategy_lines = ""
    if "strategy" in report.columns:
        parts = []
        for strat, grp in report.groupby(report["strategy"].fillna("bottom_fishing")):
            g_total = len(grp)
            g_win = len(grp[grp["return_pct"] > 0])
            g_wr = g_win / g_total * 100 if g_total > 0 else 0
            g_avg = grp["return_pct"].mean()
            label = _STRATEGY_ZH.get(str(strat), str(strat))
            parts.append(f"**{label}**: {g_total} 只 | 胜率 {g_wr:.1f}% | 平均收益 {'+' if g_avg >= 0 else ''}{g_avg:.2f}%")
        if parts:
            strategy_lines = "\n" + "\n".join(parts)

    elements = [
        _md_element(
            f"**时间:** {now}\n"
            f"**追踪数量:** {total} 只\n"
            f"**胜率:** {win_rate:.1f}% ({win}/{total})\n"
            f"**平均收益:** {avg_return:.2f}%\n"
            f"**状态分布:** {status_text}"
            f"{strategy_lines}"
        ),
        _divider(),
    ]

    # 每只股票的追踪详情
    for _, row in report.iterrows():
        r = row.to_dict()
        ret = r.get("return_pct", 0)
        emoji = "+" if ret >= 0 else ""
        strat_tag = f"[{_STRATEGY_ZH.get(str(r.get('strategy') or 'bottom_fishing'), '?')}] " \
            if "strategy" in r else ""
        elements.append(_md_element(
            f"{strat_tag}**{r.get('name', '')} {r.get('code', '')}** "
            f"| {r.get('status', '')} "
            f"| 推荐价 {r.get('rec_price', 0)} → 现价 {r.get('current_price', 0)} "
            f"| 收益 {emoji}{ret:.2f}%"
        ))

    card = {
        "header": _build_header(f"周度追踪 - 胜率{win_rate:.0f}%", color="blue"),
        "elements": elements,
    }
    _send_feishu(card)


def notify_attribution_report(stats: Optional[dict]) -> None:
    """发送信号归因月报。

    由 run_monthly_attribution.py 调用，基于 stock_recommendation + stock_tracking
    历史数据聚合的各信号维度胜率/收益统计。

    stats 格式:
        {"period": str, "tracked_recs": int, "win_rate": float, "avg_return": float,
         "avg_peak": float, "by_grade": [...], "by_divergence": [...],
         "by_market": [...], "by_week": [...]}
        其中各分组列表元素: {"label": str, "n": int, "win_rate": float,
                             "avg_return": float, "avg_peak": float}
    """
    if not stats or stats.get("tracked_recs", 0) == 0:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    win_rate = stats.get("win_rate", 0)

    def _group_lines(rows: list[dict]) -> list[str]:
        lines = []
        for g in rows:
            ret = g.get("avg_return", 0)
            peak = g.get("avg_peak", 0)
            lines.append(
                f"**{g.get('label', '')}**: {g.get('n', 0)} 只 | "
                f"胜率 {g.get('win_rate', 0):.1f}% | "
                f"平均收益 {'+' if ret >= 0 else ''}{ret:.2f}% | "
                f"平均峰值 {'+' if peak >= 0 else ''}{peak:.2f}%"
            )
        return lines

    elements = [
        _md_element(
            f"**时间:** {now}\n"
            f"**统计周期:** {stats.get('period', '')}\n"
            f"**已追踪推荐:** {stats.get('tracked_recs', 0)} 条\n"
            f"**整体胜率:** {win_rate:.1f}%\n"
            f"**平均收益:** {stats.get('avg_return', 0):.2f}%\n"
            f"**平均峰值收益:** {stats.get('avg_peak', 0):.2f}%\n"
            f"\n收益口径：每条推荐取最新一次周度追踪的收益率（31 天窗口）；峰值为追踪期内最高周度收益率。"
        ),
    ]

    sections = [
        ("按信号等级", stats.get("by_grade")),
        ("按评分分档", stats.get("by_score")),
        ("按底背离", stats.get("by_divergence")),
        ("按市场环境", stats.get("by_market")),
        ("按持有周次", stats.get("by_week")),
    ]
    for title, rows in sections:
        if not rows:
            continue
        elements.append(_divider())
        elements.append(_md_element(f"**{title}**"))
        elements.append(_md_element("\n".join(_group_lines(rows))))

    color = "green" if win_rate >= 50 else "red"
    card = {
        "header": _build_header(f"信号归因月报 - 胜率{win_rate:.0f}%", color=color),
        "elements": elements,
    }
    _send_feishu(card)

