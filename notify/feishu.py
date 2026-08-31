"""
飞书 Webhook 通知模块

通过飞书自定义机器人 Webhook 发送选股结果、追踪报告、月度绩效。
环境变量 FEISHU_WEBHOOK_URL 未配置时静默跳过。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def _send_feishu(card: dict) -> bool:
    """发送飞书消息卡片，返回是否成功。"""
    if not WEBHOOK_URL:
        print("[INFO] FEISHU_WEBHOOK_URL 未配置，跳过飞书通知")
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
            print("[INFO] 飞书通知发送成功")
            return True
        else:
            print(f"[WARN] 飞书通知返回异常: {data}")
            return False
    except Exception as e:
        print(f"[WARN] 飞书通知发送失败: {e}")
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


def notify_screening_result(
    df: Optional[pd.DataFrame],
    market_env: str = "unknown",
    error_msg: Optional[str] = None,
) -> None:
    """发送选股结果通知。

    参数匹配 run.py 中的调用:
        notify_screening_result(output_df, market_env=market_env_desc)
        notify_screening_result(None, market_env=market_env_desc, error_msg=str(e))
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 异常通知
    if error_msg:
        card = {
            "header": _build_header("选股策略执行异常", color="red"),
            "elements": [
                _md_element(f"**时间:** {now}\n**市场环境:** {market_env}"),
                _divider(),
                _md_element(f"**错误信息:**\n```\n{error_msg[:500]}\n```"),
            ],
        }
        _send_feishu(card)
        return

    # 无信号
    if df is None or df.empty:
        card = {
            "header": _build_header("选股结果 - 今日无信号", color="grey"),
            "elements": [
                _md_element(f"**时间:** {now}\n**市场环境:** {market_env}\n\n今日未发现符合5层过滤条件的标的。"),
            ],
        }
        _send_feishu(card)
        return

    # 有信号
    elements = [
        _md_element(f"**时间:** {now}\n**市场环境:** {market_env}\n**推荐数量:** {len(df)} 只"),
        _divider(),
    ]

    # 尝试导入 describe 函数
    try:
        from src.bottom_fishing_strategy import describe
    except ImportError:
        describe = None

    for _, row in df.head(10).iterrows():
        r = row.to_dict()
        if describe:
            text = describe(r)
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

    card = {
        "header": _build_header(f"选股结果 - {len(df)}只信号", color="green"),
        "elements": elements,
    }
    _send_feishu(card)


def notify_tracking_result(report: Optional[pd.DataFrame]) -> None:
    """发送周度追踪报告。

    参数匹配 run.py 中的调用:
        notify_tracking_result(report)
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

    elements = [
        _md_element(
            f"**时间:** {now}\n"
            f"**追踪数量:** {total} 只\n"
            f"**胜率:** {win_rate:.1f}% ({win}/{total})\n"
            f"**平均收益:** {avg_return:.2f}%\n"
            f"**状态分布:** {status_text}"
        ),
        _divider(),
    ]

    # 每只股票的追踪详情
    for _, row in report.iterrows():
        r = row.to_dict()
        ret = r.get("return_pct", 0)
        emoji = "+" if ret >= 0 else ""
        elements.append(_md_element(
            f"**{r.get('name', '')} {r.get('code', '')}** "
            f"| {r.get('status', '')} "
            f"| 推荐价 {r.get('rec_price', 0)} → 现价 {r.get('current_price', 0)} "
            f"| 收益 {emoji}{ret:.2f}%"
        ))

    card = {
        "header": _build_header(f"周度追踪 - 胜率{win_rate:.0f}%", color="blue"),
        "elements": elements,
    }
    _send_feishu(card)


def notify_monthly_performance(stats: Optional[dict]) -> None:
    """发送月度绩效报告。

    参数匹配 run.py 中的调用:
        notify_monthly_performance(monthly_stats)

    stats 格式:
        {"total_signals": int, "win_rate": float, "avg_return": float,
         "max_return": float, "min_return": float, "period": str, ...}
    """
    if not stats or stats.get("total_signals", 0) == 0:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = stats.get("total_signals", 0)
    win_rate = stats.get("win_rate", 0)
    avg_return = stats.get("avg_return", 0)
    max_return = stats.get("max_return", 0)
    min_return = stats.get("min_return", 0)

    elements = [
        _md_element(
            f"**时间:** {now}\n"
            f"**统计周期:** 近30天\n"
            f"**推荐总数:** {total} 只\n"
            f"**胜率:** {win_rate:.1f}%\n"
            f"**平均收益:** {avg_return:.2f}%\n"
            f"**最大收益:** {max_return:.2f}%\n"
            f"**最大亏损:** {min_return:.2f}%"
        ),
    ]

    # 等级分布
    grade_dist = stats.get("grade_distribution")
    if grade_dist:
        grade_text = " | ".join([f"{k}级: {v}只" for k, v in grade_dist.items()])
        elements.append(_divider())
        elements.append(_md_element(f"**等级分布:** {grade_text}"))

    color = "green" if win_rate >= 50 else "red"
    card = {
        "header": _build_header(f"月度绩效 - 胜率{win_rate:.0f}%", color=color),
        "elements": elements,
    }
    _send_feishu(card)
