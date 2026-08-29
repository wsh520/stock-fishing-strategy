import tempfile
import unittest
from pathlib import Path

from panic_line_scanner import find_bottom_panic_signals, load_price_points


class PanicLineScannerTest(unittest.TestCase):
    def test_find_bottom_panic_signal_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_file = Path(tmp_dir) / "prices.csv"
            csv_file.write_text(
                "\n".join(
                    [
                        "code,date,close,low",
                        "AAA,2026-01-01,10,10",
                        "AAA,2026-01-02,9,9",
                        "AAA,2026-01-03,8,8",
                        "BBB,2026-01-01,10,10",
                        "BBB,2026-01-02,10,10",
                        "BBB,2026-01-03,10,10",
                    ]
                ),
                encoding="utf-8",
            )

            by_code = load_price_points(csv_file)
            signals = find_bottom_panic_signals(by_code, window=3)

            self.assertEqual([s.code for s in signals], ["AAA"])
            self.assertEqual(signals[0].panic_line, 8.0)


if __name__ == "__main__":
    unittest.main()
