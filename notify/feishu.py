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


_STRATEGY_ZH = {"bottom_fishing": "抄底", "volume_breakout": "突破"}
_VOLATILE_STRATEGY_LABEL = {"bottom_fishing": "抄底信号", "volume_breakout": "突破信号"}
VOLATILE_CARD_TOP = 5   # 卡片内逐只展示上限（超出仅提示条数，避免卡片过长）


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
) -> None:
    """发送选股结果通知。

    参数匹配 run.py / run_breakout.py 中的调用:
        notify_screening_result(output_df, market_env=market_env_desc, pending=pending_df, strategy="bottom_fishing")
        notify_screening_result(output_df, market_env=market_env_desc, strategy="volume_breakout")
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))

    分层展示：df 为正式推荐；pending 为待核验候选
    （财务/周线数据缺失，未正式推荐，不与正式推荐混排）；
    volatile 为「波动率风控否决」的高风险观察池（技术面/形态已达标，仅 ATR 超限被拦），
    单独成区块并显式标注风险等级与风险提示，避免被误读为推荐标的。
    strategy 决定卡片标题措辞与单票描述格式（抄底=低位企稳候选 / 突破=放量突破候选）。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_breakout = strategy == "volume_breakout"
    candidate_label = "放量突破候选" if is_breakout else "低位企稳候选"
    card_title = "放量突破选股结果" if is_breakout else "选股结果"
    quality_mode = any(frame is not None and not frame.empty and "weekly_status" in frame
                       and frame["weekly_status"].eq("not_required").any()
                       for frame in (df, pending))
    if quality_mode:
        candidate_label = "优质低估低位候选"
        card_title = "优质低估低位荐股"

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
        from src.bottom_fishing_strategy import describe, describe_pending, describe_volatile
    except ImportError:
        describe = None
        describe_pending = None
        describe_volatile = None
    try:
        from src.volume_breakout_strategy import describe_breakout
    except ImportError:
        describe_breakout = None
    describe_fn = (describe_breakout or describe) if is_breakout else describe

    def _pending_elements() -> list:
        """待核验候选区块：与正式推荐分开，说明缺什么、为什么没进正式推荐"""
        if pending is None or pending.empty:
            return []
        elems = [
            _divider(),
            _md_element(f"**待核验候选 {len(pending)} 只**（必要数据待核验，未正式推荐，不参与追踪）"),
        ]
        for _, row in pending.head(5).iterrows():
            r = row.to_dict()
            if describe_pending:
                text = describe_pending(r)
            else:
                text = f"**{r.get('name', '')} {r.get('code', '')}** | 缺项: {r.get('missing_tags', '-')}"
            elems.append(_md_element(text))
        if len(pending) > 5:
            elems.append(_md_element(f"*...共 {len(pending)} 只，仅展示前5*"))
        return elems

    # 无信号
    if df is None or df.empty:
        no_signal_text = "今日无正式推荐（未发现数据完整且通过全部条件的标的），宁可少荐。"
        if pending is not None and not pending.empty:
            no_signal_text += f"\n另有待核验候选 {len(pending)} 只（数据待补全）。"
        if volatile is not None and not volatile.empty:
            no_signal_text += (f"\n本轮有 {len(volatile)} 只仅因**波动率超限**被拦下"
                               f"（技术面已达标），详见下方高风险观察池。")
        card = {
            "header": _build_header(
                f"{card_title} - 今日无信号"
                + (f"（波动率观察池 {len(volatile)} 只）" if volatile is not None and not volatile.empty else ""),
                color="grey"),
            "elements": [
                _md_element(f"**时间:** {now}\n**市场环境:** {market_env}\n\n{no_signal_text}"),
                *_pending_elements(),
                *_volatile_elements(volatile, strategy),
            ],
        }
        _send_feishu(card)
        return

    # 有信号
    elements = [
        _md_element(f"**时间:** {now}\n**市场环境:** {market_env}\n**推荐数量:** {len(df)} 只（{candidate_label}）"),
        _divider(),
    ]

    for _, row in df.head(10).iterrows():
        r = row.to_dict()
        if describe_fn:
            text = describe_fn(r)
        else:
            text = (
                f"**{r.get('name', '')} {r.get('code', '')}**\n"
                f"评分: {r.get('score', 0)} ({r.get('grade', '')}) "
                f"| 收盘: {r.get('close', 0)} "
                f"| 止损: {r.get('stop_loss', 0)} "
                f"| 止盈: {r.get('take_profit', 0)} "
                f"| RR: {r.get('rr_ratio', 0)}"
            )
        elements.append(_md_element(text))
        elements.append(_divider())

    if len(df) > 10:
        elements.append(_md_element(f"*...共 {len(df)} 只，仅展示前10*"))

    elements.extend(_pending_elements())
    elements.extend(_volatile_elements(volatile, strategy))

    card = {
        "header": _build_header(f"{card_title} - {len(df)}只信号", color="green"),
        "elements": elements,
    }
    _send_feishu(card)


def notify_tracking_result(report: Optional[pd.DataFrame]) -> None:
    """发送周度追踪报告。

    参数匹配 run_weekly_tracking.py 中的调用:
        notify_tracking_result(report)

    report 含 strategy 列时按策略来源分列统计（抄底/突破各自的数量/胜率/平均收益），
    逐只明细行也带 [抄底]/[突破] 前缀标签。
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

