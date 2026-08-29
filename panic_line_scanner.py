#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass
class PricePoint:
    code: str
    date: str
    close: float
    low: float


@dataclass
class PanicSignal:
    code: str
    date: str
    close: float
    panic_line: float

    @property
    def diff_pct(self) -> float:
        return (self.close - self.panic_line) / self.panic_line * 100


def load_price_points(csv_path: Path) -> Dict[str, List[PricePoint]]:
    by_code: Dict[str, List[PricePoint]] = defaultdict(list)
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("code") or "").strip()
            date = (row.get("date") or "").strip()
            close_text = row.get("close")
            low_text = row.get("low") or close_text
            if not code or not date or close_text is None or low_text is None:
                continue
            by_code[code].append(
                PricePoint(
                    code=code,
                    date=date,
                    close=float(close_text),
                    low=float(low_text),
                )
            )
    for points in by_code.values():
        points.sort(key=lambda p: p.date)
    return by_code


def find_bottom_panic_signals(
    by_code: Dict[str, List[PricePoint]],
    window: int = 20,
    tolerance_pct: float = 0.0,
) -> List[PanicSignal]:
    signals: List[PanicSignal] = []
    tolerance_ratio = 1 + tolerance_pct / 100
    for code, points in by_code.items():
        if len(points) < window:
            continue
        lows_window: deque[float] = deque(maxlen=window)
        panic_line = 0.0
        for point in points:
            lows_window.append(point.low)
            if len(lows_window) < window:
                continue
            panic_line = min(lows_window)
        latest = points[-1]
        if latest.close <= panic_line * tolerance_ratio and panic_line < max(lows_window):
            signals.append(
                PanicSignal(
                    code=code,
                    date=latest.date,
                    close=latest.close,
                    panic_line=panic_line,
                )
            )
    return sorted(signals, key=lambda s: (s.date, s.code))


def format_signals(signals: Iterable[PanicSignal]) -> str:
    lines = ["code,date,close,panic_line,diff_pct"]
    for signal in signals:
        lines.append(
            f"{signal.code},{signal.date},{signal.close:.2f},{signal.panic_line:.2f},{signal.diff_pct:.2f}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检索触及底部恐慌线的股票")
    parser.add_argument("csv_path", type=Path, help="包含 code,date,close,low 列的 CSV 文件")
    parser.add_argument("--window", type=int, default=20, help="恐慌线回看窗口天数，默认 20")
    parser.add_argument(
        "--tolerance-pct",
        type=float,
        default=0.0,
        help="允许高于恐慌线的偏差百分比，例如 2 表示 close <= panic_line * 1.02",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    by_code = load_price_points(args.csv_path)
    signals = find_bottom_panic_signals(
        by_code=by_code, window=args.window, tolerance_pct=args.tolerance_pct
    )
    print(format_signals(signals))


if __name__ == "__main__":
    main()
