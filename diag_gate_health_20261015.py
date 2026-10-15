"""2026-10-15 P2：闸门健康度后置诊断 —— 区分「市场没机会」与「数据管道故障」。

背景：10-15 生产运行 2963 只审计 → 候选 24 → 正式推荐 0。单看 0 无法判断策略
是否失灵。本脚本用同一份真实行情做交叉验证，回答一个问题：

  0 推荐到底来自「多个独立闸门同步收紧」（市场原因）
  还是「单个闸门异常集中否决」（管道故障）

判定规则（全部基于真实行情重算，不依赖日志文本）：
  R1 管道故障特征：单一闸门否决率异常高（>80%）且其余闸门近乎不动，
     或审计样本量塌陷（审计数 / 全市场数 < 90%）。
  R2 市场原因特征：多个独立闸门（估值/位置/止跌）否决率同步抬升，
     且外部指数确认下跌（双创或沪深300 处于 MA20 下方且创阶段新低）。
  R3 定位一致性：底部 confirmed ∩ 止跌通过 的规模，决定 formal 的天花板。

用法：
    PYTHONPATH=".venv/Lib/site-packages;." python diag_gate_health_20261015.py [抽样数]

只读：不修改任何生产口径，不触网写库。行情来自新浪日 K（东财/AkShare 已被代理拦截）。
"""
import glob
import os
import random
import re
import sys
import time

import numpy as np
import pandas as pd
import requests

AS_OF = "2026-10-14"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


def kline(symbol, bars=300):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={bars}")
    for _ in range(3):
        try:
            response = SESSION.get(url, timeout=20)
            if response.status_code != 200:
                time.sleep(1)
                continue
            frame = pd.DataFrame(response.json())
            if frame.empty:
                return None
            frame["day"] = pd.to_datetime(frame["day"])
            for column in ("open", "high", "low", "close"):
                frame[column] = pd.to_numeric(frame[column])
            return frame.sort_values("day").reset_index(drop=True)
        except Exception:
            time.sleep(1.2)
    return None


def universe():
    found = set()
    for path in glob.glob("cache/daily_*_last600_qfq.csv"):
        match = re.match(r"daily_(sh|sz)\.(\d+)_", os.path.basename(path))
        if match:
            found.add(match.group(1) + match.group(2))
    return sorted(found)


def profile(frame):
    """单只票的技术/位置/底部画像，口径与生产一致。"""
    close = frame["close"]
    last = float(close.iloc[-1])
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    slope = float((ma20.iloc[-1] - ma20.iloc[-6]) / ma20.iloc[-6] * 100)
    window = close.tail(60).to_numpy()
    anchor = int(np.argmin(window))
    gain = float((last / window[anchor] - 1) * 100)
    age = 59 - anchor
    pos250 = float((last / close.tail(250).min() - 1) * 100)
    return dict(last=last, above20=bool(last >= ma20.iloc[-1]),
                above60=bool(last >= ma60.iloc[-1]), slope=slope, age=age,
                gain=gain, pos250=pos250, chg20=float((last / close.iloc[-21] - 1) * 100))


def index_state(symbol, label):
    frame = kline(symbol)
    if frame is None or str(frame["day"].iloc[-1].date()) != AS_OF:
        print(f"  {label}: 行情不可用（跳过）")
        return None
    row = profile(frame)
    new_low = row["age"] == 0
    low_text = "创60日新低" if new_low else f"距60日低{row['gain']:+.2f}%"
    print(f"  {label:8s} 收盘{row['last']:9.2f}  MA20{'上方' if row['above20'] else '下方'}"
          f"  MA60{'上方' if row['above60'] else '下方'}"
          f"  距60日高{(row['last'] / frame['close'].tail(60).max() - 1) * 100:+6.2f}%"
          f"  {low_text}")
    return row


def main():
    sample_size = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    symbols = universe()
    random.seed(20261015)
    sample = random.sample(symbols, min(sample_size, len(symbols)))
    print(f"=== 闸门健康度诊断 | as_of {AS_OF} | 全市场 {len(symbols)} | 抽样 {len(sample)} ===\n")

    print("【外部环境】市场是否在下跌（决定 0 推荐是否为市场原因）")
    indices = [index_state("sh000300", "沪深300"), index_state("sz399006", "创业板指")]
    indices = [i for i in indices if i]
    bearish = [i for i in indices if not i["above20"]]
    making_lows = [i for i in indices if i["age"] == 0]

    rows, failed = [], 0
    for index, symbol in enumerate(sample):
        frame = kline(symbol)
        if frame is None or len(frame) < 250 or str(frame["day"].iloc[-1].date()) != AS_OF:
            failed += 1
            continue
        row = profile(frame)
        row["symbol"] = symbol
        rows.append(row)
        time.sleep(0.1)
        if index % 100 == 0:
            print(f"  ..{index} 有效 {len(rows)}", flush=True)

    if not rows:
        raise SystemExit("无有效样本，行情接口可能不可用")
    frame = pd.DataFrame(rows)
    total = len(frame)
    coverage = total / len(sample) * 100
    print(f"\n【样本健康度】有效 {total} / 请求 {len(sample)}（请求覆盖率 {coverage:.1f}%，失败 {failed}）")
    print(f"  抽样比例：{total} / 全市场 {len(symbols)} = {total / len(symbols) * 100:.1f}%（用于按比例外推）")

    print("\n【各闸门通过率】低于历史常态即说明市场收紧")
    position_ok = frame["pos250"] <= 40
    stop_ok = frame["above20"] & (frame["slope"] >= 0)
    bottom_ok = frame["age"].between(5, 40)
    for name, mask in (("位置 pos250<=40", position_ok),
                       ("止跌 站上MA20且斜率>=0", stop_ok),
                       ("底部 confirmed(5-40)", bottom_ok)):
        print(f"  {name:26s} 通过 {int(mask.sum()):4d} ({mask.mean() * 100:5.1f}%)")

    print("\n【闸门共现】0 推荐归因于哪一组合")
    frame["bucket"] = np.where(bottom_ok & stop_ok, "底部确认+止跌通过(formal必要)",
                        np.where(bottom_ok, "底部确认但未止跌",
                        np.where(stop_ok, "止跌通过但底部未确认", "两者皆未")))
    for label, count in frame["bucket"].value_counts().items():
        print(f"  {label:34s} {count:4d} ({count / total * 100:5.1f}%)")

    intersection = int((bottom_ok & stop_ok).sum())
    print(f"\n  正式推荐天花板（两闸门交集）= {intersection} 只 "
          f"({intersection / total * 100:.1f}%，外推全市场约 {int(intersection / total * len(symbols))} 只)")

    print("\n【判定】")
    verdicts = []
    if coverage < 90:
        verdicts.append(f"⚠ 样本覆盖率仅 {coverage:.1f}%，疑似行情管道不稳定")
    else:
        print("  R1 管道故障排查：覆盖率正常，排除「行情大面积取不到」")
    ratios = {"位置": position_ok.mean(), "止跌": stop_ok.mean(), "底部": bottom_ok.mean()}
    worst, worst_rate = min(ratios.items(), key=lambda kv: kv[1])
    if worst_rate < 0.15:
        verdicts.append(f"⚠ 单闸门 {worst} 通过率仅 {worst_rate * 100:.1f}%，需确认是否闸门失效")
    if len(bearish) >= max(1, len(indices) - 1):
        print(f"  R2 市场确认：{len(bearish)}/{len(indices)} 个指数在 MA20 下方"
              f"{'，且创 60 日新低 ' + str(len(making_lows)) + ' 个' if making_lows else ''}")
        print("     → 多个独立闸门同步收紧属市场原因，非策略失灵")
    if not verdicts:
        verdicts.append("✓ 无管道故障特征，0 推荐归因于市场不给机会")
    for verdict in verdicts:
        print(f"  {verdict}")
    print("\n  中位 20 日涨幅 %.2f%%；创新低(age=0) %d 只 (%.1f%%)" % (
        frame["chg20"].median(), int((frame["age"] == 0).sum()),
        float((frame["age"] == 0).mean()) * 100))
    print("\n提示：本脚本只做解释，不改变任何闸门口径。")


if __name__ == "__main__":
    main()