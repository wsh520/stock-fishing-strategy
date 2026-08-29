# A股量化选股策略 - 自动化执行系统

基于「周线左侧寻底 + 日线右侧确认」的量化选股策略，通过 GitHub Actions 每日自动执行，结果通过飞书机器人推送通知。

## 策略简介

- **市场环境过滤**：沪深300周线MA20斜率判断牛/熊/中性环境
- **周线左侧寻底**：ATR归一化跌幅 + 量能模式识别 + CCI超卖确认
- **基本面防雷**：ROE/负债率/商誉/扣非利润等否决项
- **日线右侧确认**：MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价配合
- **风险收益比过滤**：动态止损 + 止盈目标 + 最低1.5倍风险收益比

## 自动执行

GitHub Actions 每个交易日（周一至周五）北京时间 **15:10** 自动执行选股策略，执行结果通过飞书机器人推送。

## 快速部署

### 1. Fork 本仓库

点击右上角 Fork 按钮将本仓库 Fork 到你的 GitHub 账号下。

### 2. 创建飞书自定义机器人

1. 打开飞书，进入你想接收通知的**群聊**
2. 点击群聊右上角 **设置 (···)** → **群机器人** → **添加机器人**
3. 选择 **自定义机器人**
4. 填写机器人名称（如：选股助手）
5. 复制生成的 **Webhook 地址**（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/xxx`）
6. 点击完成

### 3. 配置 GitHub Secrets

1. 进入你 Fork 的仓库页面
2. 点击 **Settings** → 左侧 **Secrets and variables** → **Actions**
3. 点击 **New repository secret**
4. 添加以下 Secret：

| Name | Value |
|------|-------|
| `FEISHU_WEBHOOK_URL` | 你在第2步获取的飞书 Webhook 地址 |

### 4. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 策略将在每个交易日 15:10 自动执行

### 5. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening**
3. 点击 **Run workflow** → **Run workflow**
4. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   └── daily_screen.yml        # GitHub Actions 定时工作流
├── src/
│   └── bottom_fishing_strategy.py  # 核心策略脚本
├── notify/
│   └── feishu.py               # 飞书通知模块
├── data/
│   ├── signal_history.csv      # 推荐信号历史（自动生成）
│   └── tracking_report.csv     # 追踪报告（自动生成）
├── run.py                      # 入口脚本
├── requirements.txt            # Python 依赖
└── README.md
```

## 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# 运行
python run.py
```

也可以直接运行策略脚本（不发送通知）：

```bash
# 仅选股
python src/bottom_fishing_strategy.py screen

# 仅追踪历史推荐
python src/bottom_fishing_strategy.py track

# 完整流程
python src/bottom_fishing_strategy.py full
```

## 通知样例

选股结果推送到飞书群后，消息卡片包含：
- 市场环境判断
- 推荐股票列表（代码/名称/评分/信号等级）
- 交易级别（止损价/止盈价/风险收益比）
- 周度追踪报告（胜率/平均收益/状态分布）

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA20 | 周线 | 趋势判断 + 止盈目标 |
| ATR14 | 周线 | 跌幅标准化 + 止损缓冲 |
| MACD | 周线 | 底背离检测 |
| CCI14 | 周线 | 超卖区域确认 |
| MA5/MA10 | 日线 | 右侧确认触发 |
| EMA5/EMA10/EMA20 | 日线 | 趋势反转确认 + 金叉信号 |
| RSI14 | 日线 | 超卖/超买加减分 |

## 数据源

- **主数据源**：AkShare（东方财富）
- **备用数据源**：BaoStock（证券宝）
- 当主数据源请求失败时自动降级到备用源

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
