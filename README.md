# stock-fishing-strategy
股票底线钓鱼脚本

## 底部恐慌线检索脚本

仓库新增 `panic_line_scanner.py`，用于从历史价格 CSV 中检索触及底部恐慌线的股票。

### CSV 格式

需要包含以下列：

- `code`：股票代码
- `date`：交易日期（建议使用 `YYYY-MM-DD`）
- `close`：收盘价
- `low`：最低价（可选；缺失时自动使用 `close`）

### 使用方式

```bash
python panic_line_scanner.py /path/to/prices.csv --window 20 --tolerance-pct 1.5
```

参数说明：

- `--window`：恐慌线回看窗口天数（默认 20）
- `--tolerance-pct`：允许高于恐慌线的百分比偏差（默认 0）

脚本输出 CSV 到标准输出，列为：`code,date,close,panic_line,diff_pct`。
