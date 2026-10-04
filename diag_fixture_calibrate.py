"""临时 fixture 校准脚本（只读，不接入任何流程）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m

_DAY = "2025-06-30"


def mk(close) -> pd.DataFrame:
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=_DAY, periods=len(close)).strftime("%Y-%m-%d")
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                             close=close, volume=1e7, amount=close * 1e7,
                             peTTM=8., pbMRQ=.9))


def feats(label, close):
    cfg = m.StrategyConfig(USE_CACHE=False)
    c = m.compute_daily_signals(m._recompute_pct_chg(mk(close).copy()), cfg)
    d = c.iloc[-1]
    win = np.asarray(close, float)[-250:]
    pos = float((win < close[-1]).sum()) / len(win)
    ma20 = float(c["ma20"].iloc[-1])
    ma20p = float(c["ma20"].iloc[-6])
    macd_ok = m._macd_momentum_ok(c, cfg)
    deep_ok = m.macd_not_deeply_weak(c, cfg.MACD_WEAK_DAYS, cfg.MACD_WEAK_HIST_PCT)
    print(f"{label}")
    print(f"   pos={pos:.3f} (需<=0.40) close={close[-1]:.2f} ma20={ma20:.3f} "
          f"slope5={(ma20 - ma20p) / ma20p:+.4f} below_ma20={close[-1] < ma20} "
          f"macd_mom={macd_ok} not_deep_weak={deep_ok} tech={float(d['daily_score']):.0f}")


CASES = {
    "A 弱势反弹(旧fixture 10->10.4)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.4, 50)],
    "B 冲高回落非加速(10.8->10.3)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.8, 30), np.linspace(10.8, 10.3, 20)],
    "C P1深位(10->10.2)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.2, 50)],
    "D P1浅位(10->11.0)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 11.0, 50)],
    "E P1浅位(10->10.8)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.8, 50)],
    "F 加速下跌(11->9.8)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 35), np.linspace(11, 9.8, 15)[1:]],
    "G 站上MA20反弹(10->11)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)],
    "H 弱势反弹2(10->11->10.4)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 11.2, 25), np.linspace(11.2, 10.5, 25)],
    "I 弱势反弹3(横盘10.3)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.6, 30), np.linspace(10.6, 10.3, 20)],
    # ---- watch 层候选：需 below_ma20=True 且 macd_mom=False 且 not_deep_weak=True ----
    "W1 缓跌(20->10, 10->10.3缓降)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.9, 20), np.linspace(10.9, 10.3, 30)],
    "W2 冲高回落(10->11.5->10.6)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 11.5, 25), np.linspace(11.5, 10.6, 25)],
    "W3 下跌中继(10->10.5->10.1)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.5, 15), np.linspace(10.5, 10.1, 35)],
    "W4 长期缓跌(20->11, 11->10.4)": np.r_[np.linspace(20, 11, 250), np.linspace(11, 10.4, 50)],
    "W5 V型后回落(10->12->11.2)": np.r_[np.linspace(20, 10, 250), np.linspace(10, 12, 20), np.linspace(12, 11.2, 30)],
}

for label, close in CASES.items():
    feats(label, close)
