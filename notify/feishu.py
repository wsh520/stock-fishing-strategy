"""
飞书自定义机器人 Webhook 通知模块

飞书机器人 API 文档：
https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional, Dict, Any

import requests
import pandas as pd

logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def send_feishu_message(content: dict) -> bool:
    """
    发送消息到飞书 Webhook
    content: 飞书消息体
    返回: 是否发送成功
    """
    if not FEISHU_WEBHOOK_URL:
        logger.warning("未配置 FEISHU_WEBHOOK_URL 环境变量，跳过飞书通知")
        return False

    try:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        resp = requests.post(
            FEISHU_WEBHOOK_URL,
            headers=headers,
            data=json.dumps(content),
            timeout=10,
        )
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            logger.info("飞书通知发送成功")
            return True
        else:
            logger.warning(f"飞书通知发送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"飞书通知发送异常: {e}")
        return False


def format_stock_card(
    output_df: Optional[pd.DataFrame],
    market_env: str = "unknown",
    error_msg: str = "",
) -> dict:
    """
    将选股结果格式化为飞书卡片消息

    参数:
        output_df: 选股结果 DataFrame
        market_env: 市场环境描述
        error_msg: 错误信息（如有）
    返回:
        飞书消息体 dict
    """
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 错误情况
    if error_msg:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ 选股策略执行异常"},
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**执行时间**: {today_str}\n**错误信息**: {error_msg}",
                        },
                    }
                ],
            },
        }

    # 无信号
    if output_df is None or output_df.empty:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "📊 今日选股报告"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**执行时间**: {today_str}\n"
                                f"**市场环境**: {market_env}\n\n"
                                "---\n\n"
                                "今日无符合条件的股票信号。"
                            ),
                        },
                    }
                ],
            },
        }

    # 有信号 — 构造股票列表
    stock_lines = []
    for idx, row in output_df.iterrows():
        code = row.get("代码", "")
        name = row.get("名称", "")
        score = row.get("综合评分", 0)
        level = row.get("信号等级", "")
        price = row.get("现价", 0)
        rr = row.get("风险收益比", 0)
        stop = row.get("动态止损价", 0)
        tp1 = row.get("第一止盈价", 0)
        industry = row.get("所属行业", "")

        line = (
            f"**{idx + 1}. {code} {name}**  [{level}]\n"
            f"   现价:{price} | 评分:{score} | 风险收益比:{rr}\n"
            f"   止损:{stop} | 止盈:{tp1} | 行业:{industry}"
        )
        stock_lines.append(line)

    stocks_text = "\n\n".join(stock_lines)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📈 今日选股报告 ({len(output_df)}只)"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**执行时间**: {today_str}\n"
                            f"**市场环境**: {market_env}\n"
                            f"**信号数量**: {len(output_df)} 只\n\n"
                            "---\n\n"
                            f"{stocks_text}"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "⚠️ 本策略仅为量化研究工具，不构成投资建议。投资有风险，入市需谨慎。",
                        }
                    ],
                },
            ],
        },
    }


def format_tracking_card(report_df: Optional[pd.DataFrame]) -> Optional[dict]:
    """
    将追踪回测结果格式化为飞书卡片消息
    """
    if report_df is None or report_df.empty:
        return None

    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    valid_returns = report_df["收益率(%)"].dropna()
    if len(valid_returns) > 0:
        avg_return = valid_returns.mean()
        win_count = int((valid_returns > 0).sum())
        loss_count = int((valid_returns <= 0).sum())
        win_rate = win_count / len(valid_returns) * 100
        max_win = valid_returns.max()
        max_loss = valid_returns.min()
    else:
        avg_return = win_rate = max_win = max_loss = 0
        win_count = loss_count = 0

    # 状态分布
    status_counts = report_df["当前状态"].value_counts()
    status_lines = [f"  {s}: {c}只" for s, c in status_counts.items()]
    status_text = "\n".join(status_lines)

    # 个股明细（最多显示10只）
    detail_lines = []
    for _, r in report_df.head(10).iterrows():
        ret_str = f"{r['收益率(%)']:.1f}%" if pd.notna(r["收益率(%)"]) else "N/A"
        detail_lines.append(
            f"  {r['代码']} {r['名称']} | 收益:{ret_str} | {r['当前状态']}"
        )
    detail_text = "\n".join(detail_lines)
    if len(report_df) > 10:
        detail_text += f"\n  ... 等共 {len(report_df)} 只"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "📋 周度追踪报告"},
                "template": "wathet",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**报告时间**: {today_str}\n"
                            f"**追踪股票数**: {len(report_df)}\n\n"
                            "---\n\n"
                            f"**平均收益率**: {avg_return:.2f}%\n"
                            f"**胜率**: {win_rate:.1f}% ({win_count}胜/{loss_count}负)\n"
                            f"**最大盈利**: {max_win:.2f}%\n"
                            f"**最大亏损**: {max_loss:.2f}%\n\n"
                            f"**状态分布**:\n{status_text}\n\n"
                            "---\n\n"
                            f"**个股明细**:\n{detail_text}"
                        ),
                    },
                },
            ],
        },
    }


def notify_screening_result(
    output_df: Optional[pd.DataFrame],
    market_env: str = "unknown",
    error_msg: str = "",
) -> bool:
    """发送选股结果通知"""
    card = format_stock_card(output_df, market_env, error_msg)
    return send_feishu_message(card)


def notify_tracking_result(report_df: Optional[pd.DataFrame]) -> bool:
    """发送追踪报告通知"""
    card = format_tracking_card(report_df)
    if card is None:
        return True  # 无需发送
    return send_feishu_message(card)


def format_monthly_performance_card(stats: Dict[str, Any]) -> dict:
    """
    将月度绩效统计格式化为飞书卡片消息

    参数:
        stats: get_monthly_performance() 返回的统计字典
    返回:
        飞书消息体 dict
    """
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    period_days = stats.get("period_days", 30)

    # 涨幅 Top5 / 跌幅 Top5
    details = stats.get("details", [])
    tracked_details = [d for d in details if d.get("return_pct") is not None]
    sorted_by_return = sorted(tracked_details, key=lambda x: x["return_pct"], reverse=True)

    top_winners = sorted_by_return[:5]
    top_losers = sorted_by_return[-5:] if len(sorted_by_return) > 5 else []

    winners_text = "\n".join([
        f"  {r['code']} {r['name']} | +{r['return_pct']:.1f}% | {r.get('status', '')}"
        for r in top_winners if r["return_pct"] > 0
    ]) or "  暂无"

    losers_text = "\n".join([
        f"  {r['code']} {r['name']} | {r['return_pct']:.1f}% | {r.get('status', '')}"
        for r in reversed(top_losers) if r["return_pct"] <= 0
    ]) or "  暂无"

    # 按信号等级分组
    by_level = stats.get("by_signal_level", {})
    level_lines = []
    for level, ldata in by_level.items():
        level_lines.append(
            f"  {level}: {ldata['count']}只 | "
            f"胜率:{ldata['win_rate']:.0f}% | "
            f"平均收益:{ldata['avg_return']:.1f}%"
        )
    level_text = "\n".join(level_lines) or "  暂无数据"

    # 卡片颜色
    win_rate = stats.get("win_rate", 0)
    if win_rate >= 60:
        template = "green"
    elif win_rate >= 40:
        template = "orange"
    else:
        template = "red"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 近{period_days}天推荐股票绩效报告",
                },
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**报告时间**: {today_str}\n"
                            f"**统计周期**: 近 {period_days} 天\n\n"
                            "---\n\n"
                            f"**推荐总数**: {stats['total_signals']} 只\n"
                            f"**有效追踪**: {stats['valid_signals']} 只\n"
                            f"**胜率**: {stats['win_rate']:.1f}% "
                            f"({stats['win_count']}胜 / {stats['loss_count']}负)\n"
                            f"**平均收益率**: {stats['avg_return']:.2f}%\n"
                            f"**中位数收益**: {stats['median_return']:.2f}%\n"
                            f"**最大盈利**: +{stats['max_win']:.2f}%\n"
                            f"**最大亏损**: {stats['max_loss']:.2f}%\n\n"
                            "---\n\n"
                            f"**按信号等级**:\n{level_text}\n\n"
                            "---\n\n"
                            f"**涨幅前5**:\n{winners_text}\n\n"
                            f"**跌幅前5**:\n{losers_text}"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "⚠️ 历史表现不代表未来收益，仅供策略评估参考。",
                        }
                    ],
                },
            ],
        },
    }


def notify_monthly_performance(stats: Dict[str, Any]) -> bool:
    """发送月度绩效报告通知"""
    card = format_monthly_performance_card(stats)
    return send_feishu_message(card)
