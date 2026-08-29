"""
A股周线左侧寻底 + 日线右侧确认 量化选股策略（增强版）

策略核心逻辑：
    市场环境过滤 + 周线左侧寻底 + 基本面防雷 + 日线右侧确认 + 风险收益比过滤

前复权时变性说明：
    前复权价格会随新的除权除息重新计算历史值。
    实时选股模式使用当前前复权数据。
    历史回测如需严格一致性，应使用后复权或不复权 + 手动复权因子。

免责声明：
    本策略仅为量化研究工具，不构成投资建议。
    策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。
    实际效果必须通过严格的样本外回测验证。
    投资有风险，入市需谨慎。
"""

import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import akshare as ak
except ImportError:
    raise ImportError("请安装 akshare: pip install akshare")

try:
    import baostock as bs

    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False
    logging.getLogger(__name__).info("BaoStock 未安装，仅使用 AkShare 作为数据源。如需多源降级请: pip install baostock")

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# 策略配置
# ============================================================
@dataclass
class StrategyConfig:
    """策略参数集中配置"""

    # === 市场环境 ===
    MARKET_INDEX: str = "sh000300"
    MARKET_SLOPE_BULL: float = 0.5
    MARKET_SLOPE_FLAT: float = -0.5
    ENABLE_MARKET_FILTER: bool = True

    # === 基础过滤 ===
    MIN_PRICE: float = 3.0
    MAX_PRICE: float = 100.0
    MIN_LISTING_DAYS: int = 365
    MIN_WEEKLY_BARS: int = 52
    EXCLUDE_BOARDS: dict = field(default_factory=lambda: {
        "科创板": True,
        "北交所": True,
        "创业板": False,
    })

    # === 周线跌幅 ===
    WEEK_RETURN_MIN: float = -0.15
    WEEK_RETURN_MAX: float = -0.03
    ATR_DROP_MIN: float = 1.0
    ATR_DROP_MAX: float = 3.0

    # === 量能 ===
    VOLUME_RATIO_THRESHOLD: float = 1.5
    VOLUME_RECENT_SPIKE: float = 1.5
    VOLUME_RECOVERY_RATIO: float = 1.2
    TURNOVER_RATIO: float = 1.5
    MAX_WEEKLY_TURNOVER: float = 0.25

    # === 价格位置 ===
    POSITION_20_MAX: float = 0.35
    # 原值0.45过高：跌幅-3%~-15%的阴线收盘位置通常在0.1~0.3
    # 仅长下影线(锤子线)能达到0.45，降低到0.25允许更多底部形态通过
    CLOSE_POSITION_MIN: float = 0.25
    ALLOW_INTRADAY_BREAK: float = 0.01

    # === 基本面 ===
    MIN_ROE: float = 5.0
    MAX_DEBT_RATIO: float = 70.0
    REQUIRE_POSITIVE_CASHFLOW: bool = True
    REVENUE_GROWTH_MIN: float = -10.0
    PROFIT_GROWTH_MIN: float = -20.0

    # === 日线确认 ===
    # 原值22分过高：底部反弹初期MA5<MA10(-7分)，很难凑够22分
    # 降低到15分，让初期反弹信号（收阳+接近MA5+放量）能通过
    DAILY_CONFIRM_MIN_SCORE: int = 15

    # === 止损止盈 ===
    ATR_STOP_MULTIPLIER: float = 0.5
    MAX_STOP_LOSS_PCT: float = 0.10
    SLIPPAGE: float = 0.002

    # === 风险收益 ===
    MIN_RISK_REWARD: float = 1.5

    # === 评分 ===
    # 原值65分：结合放宽后的子评分门槛，总分门槛也需同步降低
    # 典型底部反弹初期：周线~25 + 日线~18 + 基本面~5 + RR~6 = 54
    # 设为50分让有效信号能通过，同时仍然过滤掉低质量标的
    MIN_SCORE: int = 50
    WEEKLY_MIN_SCORE: int = 15
    DAILY_MIN_SCORE: int = 15  # 与DAILY_CONFIRM_MIN_SCORE保持一致
    FUNDAMENTAL_MIN_SCORE: int = 3

    # === 行业分散 ===
    MAX_SAME_INDUSTRY: int = 3

    # === 网络 ===
    REQUEST_SLEEP_MIN: float = 0.5
    REQUEST_SLEEP_MAX: float = 1.2
    CACHE_EXPIRE_HOURS: int = 12
    MAX_API_RETRIES: int = 3
    API_RETRY_DELAY: int = 5

    # === 数据源 ===
    DATA_SOURCE_PRIMARY: str = "akshare"  # 主数据源
    DATA_SOURCE_FALLBACK: str = "baostock"  # 备用数据源
    ENABLE_FALLBACK: bool = True  # 是否启用降级

    # === 模式 ===
    MODE: str = "screen"
    USE_COMPLETED_WEEK_ONLY: bool = True


# ============================================================
# 列名映射
# ============================================================
COLUMN_ALIASES = {
    "volume": ["成交量", "volume", "Volume"],
    "turnover": ["换手率", "turnover", "Turnover"],
    "close": ["收盘", "close", "Close"],
    "open": ["开盘", "open", "Open"],
    "high": ["最高", "high", "High"],
    "low": ["最低", "low", "Low"],
    "date": ["日期", "date", "Date"],
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名为英文标准名"""
    rename_map = {}
    for standard, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in df.columns and standard not in df.columns:
                rename_map[alias] = standard
                break
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


# ============================================================
# 缓存管理
# ============================================================
class CacheManager:
    """数据缓存管理"""

    def __init__(self, base_dir: str = "data_cache", expire_hours: int = 12):
        self.base_dir = Path(base_dir)
        self.expire_hours = expire_hours
        for sub in ["weekly", "daily", "fundamental", "index"]:
            (self.base_dir / sub).mkdir(parents=True, exist_ok=True)

    def _get_path(self, category: str, key: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self.base_dir / category / f"{safe_key}.parquet"

    def is_valid(self, category: str, key: str) -> bool:
        path = self._get_path(category, key)
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.now() - mtime).total_seconds() < self.expire_hours * 3600

    def load(self, category: str, key: str) -> Optional[pd.DataFrame]:
        if not self.is_valid(category, key):
            return None
        try:
            return pd.read_parquet(self._get_path(category, key))
        except Exception:
            return None

    def save(self, category: str, key: str, df: pd.DataFrame) -> None:
        try:
            df.to_parquet(self._get_path(category, key), index=False)
        except Exception as e:
            logger.warning(f"缓存保存失败 {category}/{key}: {e}")


# ============================================================
# API 调用工具
# ============================================================
def safe_api_call(func, *args, retries: int = 3, retry_delay: int = 5, **kwargs):
    """带重试的 API 调用"""
    for i in range(retries):
        try:
            result = func(*args, **kwargs)
            if result is not None and len(result) > 0:
                return result
        except Exception as e:
            if i < retries - 1:
                logger.warning(f"API调用失败，{retry_delay}秒后重试: {e}")
                time.sleep(retry_delay)
            else:
                raise
    return None


def api_sleep(config: StrategyConfig):
    """API 请求间隔"""
    time.sleep(random.uniform(config.REQUEST_SLEEP_MIN, config.REQUEST_SLEEP_MAX))


# ============================================================
# BaoStock 数据源封装
# ============================================================
class BaoStockProvider:
    """BaoStock 数据源封装，提供与 AkShare 兼容的数据接口"""

    _logged_in = False

    @classmethod
    def login(cls):
        """登录 BaoStock（全局只需一次）"""
        if not _BS_AVAILABLE:
            return False
        if not cls._logged_in:
            try:
                lg = bs.login()
                if lg.error_code == "0":
                    cls._logged_in = True
                    logger.info("BaoStock 登录成功")
                else:
                    logger.warning(f"BaoStock 登录失败: {lg.error_msg}")
            except Exception as e:
                logger.warning(f"BaoStock 登录异常: {e}")
        return cls._logged_in

    @classmethod
    def logout(cls):
        """登出 BaoStock"""
        if cls._logged_in:
            try:
                bs.logout()
            except Exception:
                pass
            cls._logged_in = False

    @classmethod
    def _code_to_bs(cls, code: str) -> str:
        """将6位股票代码转为 BaoStock 格式 (sh.600000 / sz.000001)"""
        code = str(code).zfill(6)
        if code.startswith(("6", "9")):
            return f"sh.{code}"
        else:
            return f"sz.{code}"

    @classmethod
    def _index_to_bs(cls, index_code: str) -> str:
        """将指数代码转为 BaoStock 格式"""
        # sh000300 -> sh.000300
        if index_code.startswith("sh"):
            return f"sh.{index_code[2:]}"
        elif index_code.startswith("sz"):
            return f"sz.{index_code[2:]}"
        return index_code

    @classmethod
    def get_history(
        cls,
        code: str,
        period: str = "daily",
        start_date: str = "",
        end_date: str = "",
        adjust: str = "qfq",
    ) -> Optional[pd.DataFrame]:
        """
        获取股票历史K线数据
        code: 6位股票代码
        period: daily / weekly
        adjust: qfq(前复权) / hfq(后复权) / ""(不复权)
        """
        if not cls.login():
            return None

        bs_code = cls._code_to_bs(code)

        # BaoStock 频率映射
        freq_map = {"daily": "d", "weekly": "w"}
        frequency = freq_map.get(period, "d")

        # 复权映射
        adjust_map = {"qfq": "2", "hfq": "1", "": "3"}
        adjustflag = adjust_map.get(adjust, "2")

        # 日期格式转换 YYYYMMDD -> YYYY-MM-DD
        if start_date and len(start_date) == 8:
            start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        if end_date and len(end_date) == 8:
            end_date = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

        if not start_date:
            start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")

        fields = "date,open,high,low,close,volume,amount,turn"

        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            )
            if rs.error_code != "0":
                logger.warning(f"BaoStock 查询失败 [{code}]: {rs.error_msg}")
                return None

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=rs.fields)

            # 数值类型转换
            numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turn"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # 列名统一（turn -> turnover）
            if "turn" in df.columns:
                df = df.rename(columns={"turn": "turnover"})

            # 过滤无效行
            df = df.dropna(subset=["close"])
            df = df[df["close"] > 0].copy()

            if df.empty:
                return None

            return df

        except Exception as e:
            logger.warning(f"BaoStock 获取数据异常 [{code}]: {e}")
            return None

    @classmethod
    def get_index_history(cls, index_code: str) -> Optional[pd.DataFrame]:
        """获取指数历史数据"""
        if not cls.login():
            return None

        bs_code = cls._index_to_bs(index_code)
        start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
        end_date = datetime.now().strftime("%Y-%m-%d")

        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
            )
            if rs.error_code != "0":
                return None

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=rs.fields)
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df[df["close"] > 0].copy()

            return df if not df.empty else None
        except Exception as e:
            logger.warning(f"BaoStock 获取指数数据异常: {e}")
            return None

    @classmethod
    def get_stock_basic(cls) -> Optional[pd.DataFrame]:
        """获取全市场股票基本信息列表"""
        if not cls.login():
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        try:
            rs = bs.query_stock_basic(code_name="", code="")
            if rs.error_code != "0":
                return None

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=rs.fields)
            # 只保留A股（type=1 股票）
            df = df[df["type"] == "1"].copy()
            # 只保留上市状态 (status=1)
            df = df[df["status"] == "1"].copy()

            return df if not df.empty else None
        except Exception as e:
            logger.warning(f"BaoStock 获取股票列表异常: {e}")
            return None


# ============================================================
# 多源数据获取统一接口
# ============================================================
def fetch_stock_history(
    code: str,
    period: str = "daily",
    start_date: str = "",
    end_date: str = "",
    adjust: str = "qfq",
    config: Optional[StrategyConfig] = None,
) -> Optional[pd.DataFrame]:
    """
    统一的股票历史数据获取接口
    优先使用 AkShare，失败后降级到 BaoStock
    """
    if config is None:
        config = StrategyConfig()

    df = None

    # 主数据源: AkShare
    try:
        kwargs = {
            "symbol": str(code).zfill(6),
            "period": "weekly" if period == "weekly" else "daily",
            "adjust": adjust,
        }
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date

        df = safe_api_call(
            ak.stock_zh_a_hist,
            retries=config.MAX_API_RETRIES,
            retry_delay=config.API_RETRY_DELAY,
            **kwargs,
        )
    except Exception as e:
        logger.debug(f"AkShare 获取 {code} {period} 数据失败: {e}")

    # 降级: BaoStock
    if (df is None or df.empty) and config.ENABLE_FALLBACK and _BS_AVAILABLE:
        logger.debug(f"降级使用 BaoStock 获取 {code} {period} 数据")
        df = BaoStockProvider.get_history(
            code=str(code).zfill(6),
            period=period,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )

    return df


def fetch_index_history(
    index_code: str,
    config: Optional[StrategyConfig] = None,
) -> Optional[pd.DataFrame]:
    """
    统一的指数历史数据获取接口
    优先使用 AkShare，失败后降级到 BaoStock
    """
    if config is None:
        config = StrategyConfig()

    df = None

    # 主数据源: AkShare
    try:
        df = safe_api_call(
            ak.stock_zh_index_daily,
            symbol=index_code,
            retries=config.MAX_API_RETRIES,
            retry_delay=config.API_RETRY_DELAY,
        )
    except Exception as e:
        logger.debug(f"AkShare 获取指数 {index_code} 数据失败: {e}")

    # 降级: BaoStock
    if (df is None or df.empty) and config.ENABLE_FALLBACK and _BS_AVAILABLE:
        logger.debug(f"降级使用 BaoStock 获取指数 {index_code} 数据")
        df = BaoStockProvider.get_index_history(index_code)

    return df


# ============================================================
# 数据质量检查
# ============================================================
def check_data_quality(df: pd.DataFrame) -> pd.DataFrame:
    """数据质量检查与清洗"""
    df = df.sort_values("date").drop_duplicates(subset=["date"]).copy()

    # 基本检查
    if "high" in df.columns and "low" in df.columns:
        invalid = df["high"] < df["low"]
        if invalid.any():
            df = df[~invalid].copy()

    if "close" in df.columns:
        df = df[df["close"] > 0].copy()

    if "volume" in df.columns:
        df = df[df["volume"] >= 0].copy()

    return df


# ============================================================
# 市场环境评估
# ============================================================
def get_market_environment(config: StrategyConfig, cache: CacheManager) -> dict:
    """
    评估市场环境
    返回: {"env": str, "slope": float, "description": str, "breadth": float|None}
    """
    logger.info("=" * 60)
    logger.info("A股周线左侧寻底 + 日线右侧确认策略（增强版）")
    logger.info("=" * 60)

    # 获取指数周线数据（多源降级）
    cached = cache.load("index", config.MARKET_INDEX)
    if cached is not None:
        index_df = cached
    else:
        try:
            index_df = fetch_index_history(config.MARKET_INDEX, config)
            if index_df is None or index_df.empty:
                logger.warning("无法获取指数数据（AkShare + BaoStock 均失败），跳过市场环境过滤")
                return {"env": "neutral", "slope": 0.0, "description": "数据获取失败，默认neutral", "breadth": None}
            cache.save("index", config.MARKET_INDEX, index_df)
        except Exception as e:
            logger.warning(f"指数数据获取异常: {e}，跳过市场环境过滤")
            return {"env": "neutral", "slope": 0.0, "description": "数据获取异常，默认neutral", "breadth": None}

    index_df = normalize_columns(index_df)

    # 确保日期列正确
    if "date" in index_df.columns:
        index_df["date"] = pd.to_datetime(index_df["date"])
    elif index_df.index.name == "date" or index_df.index.dtype == "datetime64[ns]":
        index_df = index_df.reset_index()
        if index_df.columns[0] != "date":
            index_df = index_df.rename(columns={index_df.columns[0]: "date"})

    index_df = index_df.sort_values("date").copy()

    # 转为周线
    index_df.set_index("date", inplace=True)
    weekly_index = index_df["close"].resample("W-FRI").last().dropna()

    if len(weekly_index) < 25:
        logger.warning("指数周线数据不足，跳过市场环境过滤")
        return {"env": "neutral", "slope": 0.0, "description": "数据不足，默认neutral", "breadth": None}

    # 丢弃未完成周
    if config.USE_COMPLETED_WEEK_ONLY:
        today = datetime.now().date()
        last_friday = weekly_index.index[-1].date()
        # 如果最后一根周线的周五还没过去，丢弃
        if last_friday > today:
            weekly_index = weekly_index.iloc[:-1]

    # 计算 MA20
    ma20 = weekly_index.rolling(20, min_periods=20).mean()

    if len(ma20.dropna()) < 10:
        return {"env": "neutral", "slope": 0.0, "description": "MA20数据不足", "breadth": None}

    # MA20 5周斜率
    current_ma20 = ma20.iloc[-1]
    prev_5_ma20 = ma20.iloc[-6] if len(ma20) >= 6 else ma20.iloc[0]
    slope_5 = (current_ma20 - prev_5_ma20) / prev_5_ma20 * 100

    # 前一段斜率（用于判断下降速度是否减缓）
    if len(ma20) >= 11:
        prev_10_ma20 = ma20.iloc[-11]
        slope_prev_5 = (prev_5_ma20 - prev_10_ma20) / prev_10_ma20 * 100
    else:
        slope_prev_5 = slope_5

    # 判断环境
    if slope_5 > config.MARKET_SLOPE_BULL:
        env = "bull"
        desc = f"牛市环境 (MA20斜率: {slope_5:.2f}%)"
    elif slope_5 > config.MARKET_SLOPE_FLAT:
        env = "neutral"
        desc = f"中性环境 (MA20斜率: {slope_5:.2f}%)"
    elif slope_5 > slope_prev_5:
        env = "bear_mild"
        desc = f"温和熊市，下降速度减缓 (MA20斜率: {slope_5:.2f}%)"
    else:
        env = "bear_severe"
        desc = f"严重熊市，加速下降 (MA20斜率: {slope_5:.2f}%)"

    logger.info(f"市场环境: {env} ({desc})")

    return {"env": env, "slope": slope_5, "description": desc, "breadth": None}


def calculate_market_breadth(stock_df: pd.DataFrame, config: StrategyConfig, cache: CacheManager) -> Optional[float]:
    """
    市场宽度指标：计算全市场站上MA20的股票比例
    返回: 比例值(0~1)，获取失败返回None
    """
    try:
        col_code = "代码" if "代码" in stock_df.columns else "code"
        col_price = "最新价" if "最新价" in stock_df.columns else "close"

        # 从实时行情数据中取样本（随机取200只以减少API调用）
        valid_stocks = stock_df[pd.to_numeric(stock_df[col_price], errors="coerce") > 0].copy()
        sample_size = min(200, len(valid_stocks))
        if sample_size < 50:
            return None

        sample = valid_stocks.sample(n=sample_size, random_state=42)
        above_ma20_count = 0
        valid_count = 0

        for _, row in sample.iterrows():
            code = str(row[col_code]).zfill(6)
            try:
                start_date = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
                end_date = datetime.now().strftime("%Y%m%d")
                df = fetch_stock_history(
                    code=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq", config=config
                )
                if df is None or df.empty:
                    continue
                df = normalize_columns(df)
                if "close" not in df.columns or len(df) < 20:
                    continue
                ma20 = df["close"].rolling(20, min_periods=20).mean()
                last_close = df["close"].iloc[-1]
                last_ma20 = ma20.iloc[-1]
                if pd.notna(last_ma20):
                    valid_count += 1
                    if last_close > last_ma20:
                        above_ma20_count += 1
                api_sleep(config)
            except Exception:
                continue

        if valid_count < 30:
            return None

        breadth = above_ma20_count / valid_count
        logger.info(f"市场宽度指标: {breadth:.1%} 的样本股票站上MA20 (样本数:{valid_count})")
        return breadth

    except Exception as e:
        logger.warning(f"计算市场宽度指标失败: {e}")
        return None


# ============================================================
# 股票列表获取与基础过滤
# ============================================================
def is_restricted_board(code: str, config: StrategyConfig) -> bool:
    """判断股票是否属于有资产门槛要求的板块"""
    if config.EXCLUDE_BOARDS.get("科创板", True):
        if code.startswith(("688", "689")):
            return True
    if config.EXCLUDE_BOARDS.get("北交所", True):
        if code.startswith(("4", "8")):
            return True
    if config.EXCLUDE_BOARDS.get("创业板", False):
        if code.startswith(("300", "301")):
            return True
    return False


def get_stock_list(config: StrategyConfig) -> pd.DataFrame:
    """获取全市场股票列表（多源降级）"""
    logger.info("获取全市场股票列表...")

    # 主数据源: AkShare 实时行情
    df = None
    try:
        df = safe_api_call(
            ak.stock_zh_a_spot_em,
            retries=config.MAX_API_RETRIES,
            retry_delay=config.API_RETRY_DELAY,
        )
    except Exception as e:
        logger.warning(f"AkShare 获取股票列表失败: {e}")

    if df is not None and not df.empty:
        return df

    # 降级: BaoStock 获取股票基本列表
    if config.ENABLE_FALLBACK and _BS_AVAILABLE:
        logger.info("降级使用 BaoStock 获取股票列表...")
        bs_df = BaoStockProvider.get_stock_basic()
        if bs_df is not None and not bs_df.empty:
            # 转换为与 AkShare 兼容的格式
            result = pd.DataFrame()
            result["代码"] = bs_df["code"].apply(lambda x: x.split(".")[1] if "." in str(x) else x)
            result["名称"] = bs_df.get("code_name", "")
            # BaoStock 不提供实时价格，标记为需要单独获取
            result["最新价"] = np.nan
            logger.info(f"BaoStock 获取到 {len(result)} 只股票")
            return result

    raise RuntimeError("无法获取A股股票列表数据（AkShare + BaoStock 均失败）")


def get_listing_date(code: str, config: StrategyConfig) -> Optional[datetime]:
    """通过个股信息接口获取上市日期"""
    try:
        df_info = safe_api_call(
            ak.stock_individual_info_em,
            symbol=code,
            retries=2,
            retry_delay=3,
        )
        if df_info is not None and not df_info.empty:
            for _, row in df_info.iterrows():
                item = str(row.iloc[0])
                if "上市" in item:
                    val = row.iloc[1]
                    return pd.to_datetime(val)
    except Exception:
        pass
    return None


def filter_stock_list(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """基础过滤"""
    total = len(df)

    # 统一列名处理
    col_code = "代码" if "代码" in df.columns else "code"
    col_name = "名称" if "名称" in df.columns else "name"
    col_price = "最新价" if "最新价" in df.columns else "close"

    result = df.copy()

    # ST / *ST / 退市整理
    if col_name in result.columns:
        mask_st = result[col_name].str.contains(r"ST|退市", na=False, regex=True)
        result = result[~mask_st].copy()

    # 停牌 (最新价为0或NaN)
    if col_price in result.columns:
        result = result[pd.to_numeric(result[col_price], errors="coerce") > 0].copy()

    # 价格范围
    if col_price in result.columns:
        price = pd.to_numeric(result[col_price], errors="coerce")
        result = result[
            (price >= config.MIN_PRICE) & (price <= config.MAX_PRICE)
        ].copy()

    # 板块过滤
    if col_code in result.columns:
        mask_board = result[col_code].apply(lambda x: is_restricted_board(str(x), config))
        result = result[~mask_board].copy()

    logger.info(f"股票总数: {total}")
    logger.info(f"基础过滤后: {len(result)}")

    return result


# ============================================================
# 周线数据获取与指标计算
# ============================================================
def get_weekly_data(code: str, config: StrategyConfig, cache: CacheManager) -> Optional[pd.DataFrame]:
    """获取单只股票周线数据（多源降级）"""
    cached = cache.load("weekly", code)
    if cached is not None:
        return cached

    df = fetch_stock_history(code=code, period="weekly", adjust="qfq", config=config)
    if df is not None and not df.empty:
        cache.save("weekly", code, df)
        return df
    return None


def calculate_weekly_indicators(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """计算周线技术指标"""
    df = normalize_columns(df)
    df = check_data_quality(df)

    if len(df) < config.MIN_WEEKLY_BARS:
        return pd.DataFrame()

    # 丢弃未完成周：如果最后一根周线的周五尚未过去，丢弃
    if config.USE_COMPLETED_WEEK_ONLY and len(df) > 0:
        df["date"] = pd.to_datetime(df["date"])
        today = datetime.now().date()
        last_date = df["date"].iloc[-1].date()
        # 计算该周的周五日期
        last_weekday = last_date.weekday()  # 0=Mon, 4=Fri
        # 该周的周五 = last_date + (4 - last_weekday) 天
        friday_of_last_bar = last_date + timedelta(days=(4 - last_weekday))
        if friday_of_last_bar > today:
            df = df.iloc[:-1].copy()

    if len(df) < config.MIN_WEEKLY_BARS:
        return pd.DataFrame()

    # MA20
    df["MA20"] = df["close"].rolling(20, min_periods=20).mean()

    # MA20的5周均线
    df["MA20_MA5"] = df["MA20"].rolling(5, min_periods=5).mean()

    # 前12周平均成交量（排除当前周）
    df["volume_ma12_prev"] = df["volume"].shift(1).rolling(12, min_periods=12).mean()

    # 前12周平均换手率（排除当前周）
    if "turnover" in df.columns:
        df["turnover_ma12_prev"] = df["turnover"].shift(1).rolling(12, min_periods=12).mean()
    else:
        df["turnover_ma12_prev"] = np.nan

    # 前4周平均成交量
    df["volume_ma4_prev"] = df["volume"].shift(1).rolling(4, min_periods=4).mean()

    # 前20周最低价（排除当前周）
    df["prev_20_low"] = df["low"].shift(1).rolling(20, min_periods=20).min()

    # 前20周最高价（排除当前周）
    df["prev_20_high"] = df["high"].shift(1).rolling(20, min_periods=20).max()

    # 20周价格位置
    price_range = df["prev_20_high"] - df["prev_20_low"]
    df["position_20"] = np.where(
        price_range > 0,
        (df["close"] - df["prev_20_low"]) / price_range,
        np.nan,
    )

    # 本周涨跌幅
    df["week_return"] = df["close"].pct_change()

    # 本周K线实体比例
    hl_range = df["high"] - df["low"]
    df["body_ratio"] = np.where(
        hl_range > 0,
        (df["close"] - df["open"]) / hl_range,
        np.nan,
    )

    # 本周收盘位置
    df["close_position"] = np.where(
        hl_range > 0,
        (df["close"] - df["low"]) / hl_range,
        np.nan,
    )

    # 下影线比例
    lower_body = np.minimum(df["open"], df["close"])
    df["lower_shadow_ratio"] = np.where(
        hl_range > 0,
        (lower_body - df["low"]) / hl_range,
        np.nan,
    )

    # ATR14
    df["TR"] = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = df["TR"].rolling(14, min_periods=14).mean()
    df["atr_ratio"] = np.where(df["close"] > 0, df["ATR14"] / df["close"], np.nan)

    # 量比
    df["volume_ratio"] = np.where(
        df["volume_ma12_prev"] > 0,
        df["volume"] / df["volume_ma12_prev"],
        np.nan,
    )

    # 换手比
    if "turnover" in df.columns:
        df["turnover_ratio"] = np.where(
            df["turnover_ma12_prev"] > 0,
            df["turnover"] / df["turnover_ma12_prev"],
            np.nan,
        )
    else:
        df["turnover_ratio"] = np.nan

    # MACD
    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = df["EMA12"] - df["EMA26"]
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = (df["DIF"] - df["DEA"]) * 2

    # CCI14（商品通道指数）
    tp = (df["high"] + df["low"] + df["close"]) / 3  # 典型价格
    tp_ma = tp.rolling(14, min_periods=14).mean()
    tp_md = tp.rolling(14, min_periods=14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df["CCI14"] = np.where(tp_md > 0, (tp - tp_ma) / (0.015 * tp_md), 0.0)

    return df


def check_weekly_bottom(df: pd.DataFrame, config: StrategyConfig) -> dict:
    """
    周线底部形态识别
    返回: {"pass": bool, "score": int, "details": dict}
    """
    if df.empty or len(df) < config.MIN_WEEKLY_BARS:
        return {"pass": False, "score": 0, "details": {}}

    T = df.iloc[-1]
    details = {}
    score = 0

    # ---- 必须条件：下跌趋势 ----
    if pd.isna(T["MA20"]):
        return {"pass": False, "score": 0, "details": {"reason": "MA20数据不足"}}

    # Close < MA20
    if T["close"] >= T["MA20"]:
        return {"pass": False, "score": 0, "details": {"reason": "收盘价未低于MA20"}}

    # MA20[T] < MA20[T-5]
    if len(df) < 6:
        return {"pass": False, "score": 0, "details": {"reason": "数据不足以计算MA20斜率"}}
    ma20_t5 = df["MA20"].iloc[-6]
    if pd.isna(ma20_t5) or T["MA20"] >= ma20_t5:
        return {"pass": False, "score": 0, "details": {"reason": "MA20未处于下降趋势"}}

    # ---- 周线跌幅（ATR归一化） ----
    week_ret = T["week_return"]
    atr_ratio = T["atr_ratio"]

    if pd.isna(week_ret) or pd.isna(atr_ratio) or atr_ratio <= 0:
        return {"pass": False, "score": 0, "details": {"reason": "涨跌幅或ATR数据缺失"}}

    # 基本跌幅门槛
    if week_ret < config.WEEK_RETURN_MIN or week_ret > config.WEEK_RETURN_MAX:
        return {"pass": False, "score": 0, "details": {"reason": f"周跌幅{week_ret:.2%}不在范围内"}}

    # ATR归一化
    normalized_drop = abs(week_ret) / atr_ratio
    if normalized_drop < config.ATR_DROP_MIN or normalized_drop > config.ATR_DROP_MAX:
        return {"pass": False, "score": 0, "details": {"reason": f"ATR归一化跌幅{normalized_drop:.2f}不在范围内"}}

    details["normalized_drop"] = normalized_drop

    # ---- 量能条件 ----
    vol_ratio = T.get("volume_ratio", np.nan)
    if pd.isna(vol_ratio):
        return {"pass": False, "score": 0, "details": {"reason": "量能数据缺失"}}

    # 模式A: 放量承接（量比 >= 1.5）
    mode_a = vol_ratio >= config.VOLUME_RATIO_THRESHOLD

    # 模式B: 缩量企稳后温和回升
    # 放宽：原来要求三个子条件全部成立，改为满足任意两个即可
    mode_b = False
    volume_mode = "none"
    if len(df) >= 4:
        vol_t1 = df["volume"].iloc[-2]
        vol_t2 = df["volume"].iloc[-3]
        vol_ma12_t1 = df["volume_ma12_prev"].iloc[-2]
        vol_ma12_t2 = df["volume_ma12_prev"].iloc[-3]
        vol_ma4 = T.get("volume_ma4_prev", np.nan)

        # 子条件1: 前期放量
        recent_spike = False
        if not pd.isna(vol_ma12_t1) and vol_ma12_t1 > 0:
            if vol_t1 >= vol_ma12_t1 * config.VOLUME_RECENT_SPIKE:
                recent_spike = True
        if not recent_spike and not pd.isna(vol_ma12_t2) and vol_ma12_t2 > 0:
            if vol_t2 >= vol_ma12_t2 * config.VOLUME_RECENT_SPIKE:
                recent_spike = True

        # 子条件2: 当前温和放量
        current_moderate = False
        vol_ma12 = T["volume_ma12_prev"]
        if not pd.isna(vol_ma12) and vol_ma12 > 0:
            current_moderate = (
                T["volume"] >= vol_ma12 * 0.8 and
                T["volume"] < vol_ma12 * 2.5
            )

        # 子条件3: 相对近期回升
        recent_recovery = False
        if not pd.isna(vol_ma4) and vol_ma4 > 0:
            recent_recovery = (T["volume"] / vol_ma4) >= config.VOLUME_RECOVERY_RATIO

        # 放宽：三个子条件满足任意两个即可
        mode_b_score = int(recent_spike) + int(current_moderate) + int(recent_recovery)
        mode_b = mode_b_score >= 2

    # 模式C: 量比虽不到1.5但 >= 1.0（不缩量即可），作为最宽松的通过条件
    mode_c = vol_ratio >= 1.0

    if mode_a:
        volume_mode = "A"
    elif mode_b:
        volume_mode = "B"
    elif mode_c:
        volume_mode = "C"
    else:
        return {"pass": False, "score": 0, "details": {"reason": f"量能条件不满足(量比:{vol_ratio:.2f})"}}

    details["volume_mode"] = volume_mode

    # ---- 换手率条件 ----
    # 仅保留周换手率上限作为淘汰项（防止游资爆炒）
    # 换手比 >= 1.5 改为评分项而非淘汰项（缩量阴跌也是有效底部形态）
    turnover_ratio = T.get("turnover_ratio", np.nan)
    if "turnover" in df.columns and not pd.isna(T.get("turnover", np.nan)):
        weekly_turnover = T["turnover"] / 100.0 if T["turnover"] > 1 else T["turnover"]
        if weekly_turnover > config.MAX_WEEKLY_TURNOVER:
            return {"pass": False, "score": 0, "details": {"reason": f"周换手率过高:{weekly_turnover:.2%}"}}

    # ---- 不破前低 ----
    prev_20_low = T["prev_20_low"]
    if pd.isna(prev_20_low):
        return {"pass": False, "score": 0, "details": {"reason": "前20周低点数据缺失"}}

    # 允许盘中微幅破位后收回
    if T["low"] < prev_20_low * (1 - config.ALLOW_INTRADAY_BREAK):
        return {"pass": False, "score": 0, "details": {"reason": "跌破前20周低点超过容忍范围"}}
    if T["close"] < prev_20_low:
        return {"pass": False, "score": 0, "details": {"reason": "收盘价低于前20周低点"}}

    # ---- 20周价格位置 ----
    pos_20 = T["position_20"]
    if pd.isna(pos_20):
        return {"pass": False, "score": 0, "details": {"reason": "20周价格位置无法计算"}}
    if pos_20 > config.POSITION_20_MAX:
        return {"pass": False, "score": 0, "details": {"reason": f"20周价格位置过高:{pos_20:.2f}"}}

    # ---- 收盘位置 ----
    close_pos = T["close_position"]
    if pd.isna(close_pos):
        return {"pass": False, "score": 0, "details": {"reason": "收盘位置无法计算"}}
    if close_pos < config.CLOSE_POSITION_MIN:
        return {"pass": False, "score": 0, "details": {"reason": f"收盘位置过低:{close_pos:.2f}"}}

    # ============ 评分 ============
    # 不破20周低点
    score += 10
    details["not_break_low"] = True

    # 20周价格位置低位
    score += 8
    details["low_position"] = pos_20

    # 量能模式（A/B给满分，C模式量能信号较弱给少分）
    if volume_mode == "A":
        score += 6
        details["volume_score"] = 6
    elif volume_mode == "B":
        score += 5
        details["volume_score"] = 5
    else:  # Mode C
        score += 3
        details["volume_score"] = 3

    # 换手率异常
    if not pd.isna(turnover_ratio) and turnover_ratio >= config.TURNOVER_RATIO:
        score += 4

    # 收盘位置评分
    if close_pos >= 0.70:
        score += 6
    elif close_pos >= 0.60:
        score += 4
    else:
        score += 2

    # MA20下降速度减缓
    if len(df) >= 11:
        ma20_t10 = df["MA20"].iloc[-11]
        if not pd.isna(ma20_t10) and not pd.isna(ma20_t5) and ma20_t10 > 0 and ma20_t5 > 0:
            slope_5 = (T["MA20"] - ma20_t5) / ma20_t5 * 100
            slope_10 = (ma20_t5 - ma20_t10) / ma20_t10 * 100
            if slope_5 < 0 and slope_5 > slope_10:
                score += 5
                details["slope_decelerating"] = True

    # 下影线承接
    lower_shadow = T.get("lower_shadow_ratio", np.nan)
    if not pd.isna(lower_shadow) and lower_shadow >= 0.4:
        score += 3
        details["lower_shadow_strong"] = True

    # 缩量后放量模式
    if len(df) >= 4:
        vol_t1_val = df["volume"].iloc[-2]
        vol_t2_val = df["volume"].iloc[-3]
        vol_t3_val = df["volume"].iloc[-4]
        shrinking = (vol_t1_val < vol_t2_val) and (vol_t2_val < vol_t3_val)
        vol_ma12_cur = T["volume_ma12_prev"]
        current_expanding = False
        if not pd.isna(vol_ma12_cur) and vol_ma12_cur > 0:
            current_expanding = T["volume"] >= vol_ma12_cur * 1.3
        if shrinking and current_expanding:
            score += 4
            details["shrink_then_expand"] = True

    # MACD底背离（简化版：DIF拐头）
    if len(df) >= 3:
        dif_t = T["DIF"]
        dif_t1 = df["DIF"].iloc[-2]
        dif_t2 = df["DIF"].iloc[-3]
        if not pd.isna(dif_t) and not pd.isna(dif_t1) and not pd.isna(dif_t2):
            if dif_t > dif_t1 and dif_t1 < dif_t2 and dif_t < 0:
                score += 5
                details["macd_divergence"] = True

    # ATR归一化跌幅合理
    if 1.0 <= normalized_drop <= 2.0:
        score += 3
    elif 2.0 < normalized_drop <= 3.0:
        score += 0

    # CCI超卖区域确认
    if not pd.isna(T.get("CCI14")):
        cci_val = T["CCI14"]
        if len(df) >= 2:
            cci_prev = df["CCI14"].iloc[-2]
            # CCI < -100 且开始回升（从超卖区拐头）
            if not pd.isna(cci_prev) and cci_val < -100 and cci_val > cci_prev:
                score += 4
                details["cci_oversold_reversal"] = True
                # CCI < -200 极度超卖额外加分
                if cci_prev < -200:
                    score += 2
                    details["cci_extreme_oversold"] = True
        elif cci_val < -100:
            score += 2
            details["cci_oversold"] = True

    # 封顶46分
    score = min(score, 46)

    details["weekly_score"] = score

    return {"pass": True, "score": score, "details": details}


# ============================================================
# 基本面数据获取与评分
# ============================================================
def get_fundamental_data(code: str, config: StrategyConfig, cache: CacheManager) -> Optional[dict]:
    """获取基本面数据（多源降级）"""
    cached = cache.load("fundamental", code)
    if cached is not None and not cached.empty:
        return cached.iloc[0].to_dict()

    # 方案1: AkShare 同花顺财务摘要
    try:
        df_fin = safe_api_call(
            ak.stock_financial_abstract_ths,
            symbol=code,
            retries=config.MAX_API_RETRIES,
            retry_delay=config.API_RETRY_DELAY,
        )
        if df_fin is not None and not df_fin.empty:
            cache.save("fundamental", code, df_fin.head(1))
            return df_fin.iloc[0].to_dict()
    except Exception:
        pass

    # 方案2: AkShare 个股信息
    try:
        df_ind = safe_api_call(
            ak.stock_individual_info_em,
            symbol=code,
            retries=config.MAX_API_RETRIES,
            retry_delay=config.API_RETRY_DELAY,
        )
        if df_ind is not None and not df_ind.empty:
            info_dict = {}
            for _, row in df_ind.iterrows():
                info_dict[row.iloc[0]] = row.iloc[1]
            return info_dict
    except Exception:
        pass

    # 方案3: BaoStock 盈利能力数据
    if config.ENABLE_FALLBACK and _BS_AVAILABLE:
        try:
            if BaoStockProvider.login():
                bs_code = BaoStockProvider._code_to_bs(code)
                # 获取最近一年的季报盈利数据
                year = datetime.now().year
                quarter = max(1, (datetime.now().month - 1) // 3)
                rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)
                if rs.error_code != "0":
                    # 尝试上一季度
                    if quarter > 1:
                        rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter - 1)
                    else:
                        rs = bs.query_profit_data(code=bs_code, year=year - 1, quarter=4)

                if rs.error_code == "0":
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if rows:
                        bs_df = pd.DataFrame(rows, columns=rs.fields)
                        if not bs_df.empty:
                            row_data = bs_df.iloc[-1]
                            fund_dict = {}
                            # 映射BaoStock字段到策略所需字段
                            if "roeAvg" in row_data.index:
                                val = pd.to_numeric(row_data["roeAvg"], errors="coerce")
                                if pd.notna(val):
                                    fund_dict["净资产收益率"] = val * 100  # 转为百分比
                            if "netProfit" in row_data.index:
                                val = pd.to_numeric(row_data["netProfit"], errors="coerce")
                                if pd.notna(val):
                                    fund_dict["净利润"] = val
                            if fund_dict:
                                logger.debug(f"BaoStock 获取 {code} 基本面数据成功")
                                return fund_dict
        except Exception:
            pass

    return None


def check_fundamental(data: Optional[dict], config: StrategyConfig) -> dict:
    """
    基本面条件检查（通过/不通过判断）
    返回: {"pass": bool, "score": int, "details": dict}
    """
    if data is None:
        # 数据获取失败时不直接淘汰，给予最低分通过
        # 原因：AkShare/BaoStock 财务接口不稳定，不应因数据源问题淘汰技术面合格的股票
        return {"pass": True, "score": 3, "details": {"reason": "基本面数据缺失，给予最低分通过"}}

    details = {}

    # 尝试提取各指标（兼容多种数据格式）
    roe = _extract_numeric(data, ["净资产收益率", "ROE", "roe", "加权净资产收益率"])
    debt_ratio = _extract_numeric(data, ["资产负债率", "debt_ratio", "资产负债率(%)"])
    cashflow = _extract_numeric(data, ["经营现金流量净额", "经营活动产生的现金流量净额", "operating_cashflow"])
    revenue_growth = _extract_numeric(data, ["营业收入同比增长率", "营收同比", "revenue_growth", "营业总收入同比增长率"])
    profit_growth = _extract_numeric(data, ["净利润同比增长率", "净利润同比", "profit_growth", "归属净利润同比增长率"])
    goodwill = _extract_numeric(data, ["商誉", "goodwill"])
    net_assets = _extract_numeric(data, ["净资产", "股东权益合计", "net_assets"])
    deducted_profit = _extract_numeric(data, ["扣非净利润", "扣除非经常性损益后的净利润", "deducted_net_profit"])

    # === 否决项 ===
    if roe is not None and roe < 0:
        return {"pass": False, "score": 0, "details": {"reason": f"ROE为负:{roe:.1f}%"}}

    if debt_ratio is not None and debt_ratio > 80:
        return {"pass": False, "score": 0, "details": {"reason": f"资产负债率过高:{debt_ratio:.1f}%"}}

    # 商誉/净资产 < 30%（防商誉暴雷）
    if goodwill is not None and net_assets is not None and net_assets > 0:
        goodwill_ratio = goodwill / net_assets
        if goodwill_ratio > 0.30:
            return {"pass": False, "score": 0, "details": {"reason": f"商誉占净资产比例过高:{goodwill_ratio:.1%}"}}
        details["goodwill_ratio"] = goodwill_ratio

    # 扣非净利润 > 0（排除靠非经常损益维持的公司）
    if deducted_profit is not None and deducted_profit <= 0:
        return {"pass": False, "score": 0, "details": {"reason": f"扣非净利润为负:{deducted_profit:.2f}"}}

    # === 基本面条件检查 ===
    if roe is not None:
        if roe < config.MIN_ROE:
            return {"pass": False, "score": 0, "details": {"reason": f"ROE过低:{roe:.1f}%"}}
        details["ROE"] = roe

    if debt_ratio is not None:
        if debt_ratio > config.MAX_DEBT_RATIO:
            return {"pass": False, "score": 0, "details": {"reason": f"资产负债率:{debt_ratio:.1f}%"}}
        details["debt_ratio"] = debt_ratio

    if cashflow is not None:
        details["cashflow"] = cashflow
        if config.REQUIRE_POSITIVE_CASHFLOW and cashflow <= 0:
            return {"pass": False, "score": 0, "details": {"reason": "经营现金流为负"}}

    if revenue_growth is not None:
        details["revenue_growth"] = revenue_growth
        if revenue_growth < config.REVENUE_GROWTH_MIN:
            return {"pass": False, "score": 0, "details": {"reason": f"营收下滑过大:{revenue_growth:.1f}%"}}

    if profit_growth is not None:
        details["profit_growth"] = profit_growth
        if profit_growth < config.PROFIT_GROWTH_MIN:
            return {"pass": False, "score": 0, "details": {"reason": f"净利润下滑过大:{profit_growth:.1f}%"}}

    # 通过基本条件，进入评分
    score = score_fundamental(data, details, config)
    details["fundamental_score"] = score

    return {"pass": True, "score": score, "details": details}


def score_fundamental(data: dict, details: dict, config: StrategyConfig) -> int:
    """
    基本面分级评分（独立评分函数）
    返回: 基本面得分（0~15）
    """
    score = 0

    roe = details.get("ROE", _extract_numeric(data, ["净资产收益率", "ROE", "roe", "加权净资产收益率"]))
    cashflow = details.get("cashflow", _extract_numeric(data, ["经营现金流量净额", "经营活动产生的现金流量净额"]))
    revenue_growth = details.get("revenue_growth", _extract_numeric(data, ["营业收入同比增长率", "营收同比"]))
    profit_growth = details.get("profit_growth", _extract_numeric(data, ["净利润同比增长率", "净利润同比"]))

    # ROE 评分
    if roe is not None:
        if roe >= 15:
            score += 5
        elif roe >= 10:
            score += 3
        elif roe >= 5:
            score += 1

    # 经营现金流评分
    if cashflow is not None:
        if cashflow > 0:
            score += 1
            # 如果有净利润数据，计算现金流质量
            net_profit = _extract_numeric(data, ["净利润", "net_profit", "归属净利润"])
            if net_profit is not None and net_profit > 0:
                if cashflow / net_profit >= 1.0:
                    score += 2  # 额外+2（总现金流+3）

    # 营收同比评分
    if revenue_growth is not None:
        if revenue_growth > 10:
            score += 3
        elif revenue_growth > 0:
            score += 2
        elif revenue_growth > -10:
            score += 1

    # 净利润同比评分
    if profit_growth is not None:
        if profit_growth > 10:
            score += 2
        elif profit_growth > 0:
            score += 1

    # 封顶15分
    score = min(score, 15)

    # 如果所有财务数据都获取失败，给一个基础分
    if roe is None and cashflow is None and revenue_growth is None:
        score = 5
        details["note"] = "部分财务数据不可用，给予基础分"

    return score


def _extract_numeric(data: dict, keys: list) -> Optional[float]:
    """从字典中尝试提取数值"""
    for key in keys:
        if key in data:
            val = data[key]
            if isinstance(val, (int, float)):
                if not np.isnan(val):
                    return float(val)
            if isinstance(val, str):
                try:
                    val_clean = val.replace("%", "").replace(",", "").replace("亿", "").strip()
                    return float(val_clean)
                except (ValueError, TypeError):
                    continue
    return None


# ============================================================
# 日线数据获取与指标计算
# ============================================================
def get_daily_data(code: str, config: StrategyConfig, cache: CacheManager) -> Optional[pd.DataFrame]:
    """获取日线数据（多源降级）"""
    cached = cache.load("daily", code)
    if cached is not None:
        return cached

    # 获取最近120天数据（确保60个交易日）
    start_date = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    df = fetch_stock_history(
        code=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq", config=config
    )
    if df is not None and not df.empty:
        cache.save("daily", code, df)
        return df
    return None


def calculate_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算日线技术指标"""
    df = normalize_columns(df)
    df = check_data_quality(df)

    if len(df) < 20:
        return pd.DataFrame()

    df["MA5"] = df["close"].rolling(5, min_periods=5).mean()
    df["MA10"] = df["close"].rolling(10, min_periods=10).mean()
    df["MA20"] = df["close"].rolling(20, min_periods=20).mean()

    # Volume MA5
    df["volume_ma5"] = df["volume"].rolling(5, min_periods=5).mean()

    # RSI14
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14, min_periods=14).mean()
    avg_loss = loss.rolling(14, min_periods=14).mean()
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    df["RSI14"] = 100 - (100 / (1 + rs))

    # EMA均线（指数移动平均，对近期价格更敏感）
    df["EMA5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["EMA10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()

    return df


def check_right_side_confirmation(df: pd.DataFrame, config: StrategyConfig) -> dict:
    """
    日线右侧确认：检查必须条件
    返回: {"pass": bool, "score": int, "details": dict}
    """
    if df.empty or len(df) < 5:
        return {"pass": False, "score": 0, "details": {"reason": "日线数据不足"}}

    T = df.iloc[-1]
    T1 = df.iloc[-2]

    ma5 = T.get("MA5", np.nan)
    ma10 = T.get("MA10", np.nan)
    ma5_prev = T1.get("MA5", np.nan) if "MA5" in df.columns else np.nan

    if pd.isna(ma5) or pd.isna(ma10):
        return {"pass": False, "score": 0, "details": {"reason": "均线数据不足"}}

    # === 必须条件（放宽版）===
    # 原逻辑要求 Close > MA5，但周线刚大跌后日线不可能立刻站上MA5
    # 放宽为：满足以下任一即可进入评分
    #   A) Close > MA5（标准右侧确认）
    #   B) Close > MA5 * 0.98 且当日收阳且 Close > 前日Close（初期反弹信号）
    #   C) 突破前日高点（不要求站上MA5）
    ma5_turn_up = (not pd.isna(ma5_prev)) and (ma5 > ma5_prev)
    break_prev_high = T["close"] > T1["high"]
    close_above_ma5 = T["close"] > ma5
    close_near_ma5 = T["close"] > ma5 * 0.98 and T["close"] > T["open"] and T["close"] > T1["close"]

    if not close_above_ma5 and not close_near_ma5 and not break_prev_high:
        return {"pass": False, "score": 0, "details": {"reason": "未满足日线反弹条件(未站上MA5/未接近MA5收阳/未突破前高)"}}

    # 如果只是接近MA5（条件B），或仅突破前高，不再要求MA5拐头
    if not close_above_ma5 and not ma5_turn_up and not break_prev_high and not close_near_ma5:
        return {"pass": False, "score": 0, "details": {"reason": "反弹力度不足"}}

    # 通过必须条件后，进入加权评分
    score, details = score_daily_confirmation(df, config)
    details["total_daily_score"] = score
    passed = score >= config.DAILY_CONFIRM_MIN_SCORE

    return {"pass": passed, "score": score, "details": details}


def score_daily_confirmation(df: pd.DataFrame, config: StrategyConfig) -> tuple:
    """
    日线确认加权评分（独立评分函数）
    返回: (score: int, details: dict)
    """
    T = df.iloc[-1]
    T1 = df.iloc[-2]
    details = {}
    score = 0

    ma5 = T.get("MA5", np.nan)
    ma10 = T.get("MA10", np.nan)
    ma5_prev = T1.get("MA5", np.nan) if "MA5" in df.columns else np.nan

    # Close > MA5 (8分)
    if not pd.isna(ma5) and T["close"] > ma5:
        score += 8
        details["above_ma5"] = True

    # MA5 > MA10 (7分)
    if not pd.isna(ma5) and not pd.isna(ma10) and ma5 > ma10:
        score += 7
        details["ma5_above_ma10"] = True

    # MA5拐头向上 (5分)
    ma5_turn_up = (not pd.isna(ma5_prev)) and (not pd.isna(ma5)) and (ma5 > ma5_prev)
    if ma5_turn_up:
        score += 5
        details["ma5_turning_up"] = True

    # Close > 前日High (6分)
    if T["close"] > T1["high"]:
        score += 6
        details["break_prev_high"] = True

    # Volume >= Volume_MA5 (6分)
    vol_ma5 = T.get("volume_ma5", np.nan)
    if not pd.isna(vol_ma5) and vol_ma5 > 0 and T["volume"] >= vol_ma5:
        score += 6
        details["volume_confirm"] = True

    # 当日收阳 (3分)
    if T["close"] > T["open"]:
        score += 3
        details["bullish_candle"] = True

    details["daily_base_score"] = score

    # === RSI 加减分 ===
    rsi = T.get("RSI14", np.nan)
    rsi_prev = T1.get("RSI14", np.nan) if "RSI14" in df.columns else np.nan
    rsi_bonus = 0
    if not pd.isna(rsi) and not pd.isna(rsi_prev):
        if rsi > rsi_prev and rsi_prev < 35:
            rsi_bonus = 3
        elif rsi > rsi_prev and rsi_prev < 45:
            rsi_bonus = 1
        if rsi > 70:
            rsi_bonus = -2
    details["rsi_bonus"] = rsi_bonus

    # === 反包形态 ===
    engulf_bonus = 0
    if T["close"] > T["open"] and T["close"] > T1["high"]:
        body = T["close"] - T["open"]
        prev_range = T1["high"] - T1["low"]
        if prev_range > 0 and body > prev_range * 0.5:
            engulf_bonus = 4
            details["engulfing_strong"] = True
        else:
            engulf_bonus = 2
            details["engulfing_weak"] = True
    details["engulf_bonus"] = engulf_bonus

    # === EMA 确认加分 ===
    ema_bonus = 0
    ema5 = T.get("EMA5", np.nan)
    ema10 = T.get("EMA10", np.nan)
    ema20 = T.get("EMA20", np.nan)
    ema5_prev = T1.get("EMA5", np.nan) if "EMA5" in df.columns else np.nan
    ema10_prev = T1.get("EMA10", np.nan) if "EMA10" in df.columns else np.nan

    if not pd.isna(ema5) and not pd.isna(ema20):
        # EMA5 > EMA20 且 EMA5 拐头向上（短期趋势反转确认）
        if ema5 > ema20 and not pd.isna(ema5_prev) and ema5 > ema5_prev:
            ema_bonus += 5
            details["ema5_above_ema20_turning_up"] = True
        # EMA5/EMA10 金叉（EMA5 从下方上穿 EMA10）
        elif (not pd.isna(ema10) and not pd.isna(ema5_prev) and not pd.isna(ema10_prev)
              and ema5 > ema10 and ema5_prev <= ema10_prev):
            ema_bonus += 4
            details["ema_golden_cross"] = True
        # 价格站上 EMA20（中期趋势支撑）
        elif T["close"] > ema20:
            ema_bonus += 3
            details["above_ema20"] = True
    details["ema_bonus"] = ema_bonus

    # 最终日线得分（含额外加分），封顶40分
    total = min(score + rsi_bonus + engulf_bonus + ema_bonus, 40)

    return total, details


# ============================================================
# 交易价格与风险收益
# ============================================================
def calculate_trade_levels(
    weekly_df: pd.DataFrame,
    daily_df: pd.DataFrame,
    config: StrategyConfig,
) -> dict:
    """计算止损、止盈、买入价等交易价位"""
    T_weekly = weekly_df.iloc[-1]
    T_daily = daily_df.iloc[-1]
    T1_daily = daily_df.iloc[-2]

    # 买入触发价 = 日MA5
    buy_price = T_daily.get("MA5", T_daily["close"])

    # 动态止损
    prev_20_low = T_weekly["prev_20_low"]
    current_week_low = T_weekly["low"]
    atr14 = T_weekly["ATR14"]

    if pd.isna(prev_20_low) or pd.isna(atr14):
        base_stop = current_week_low * 0.97
        atr_buffer = 0
    else:
        base_stop = max(prev_20_low, current_week_low)
        atr_buffer = atr14 * config.ATR_STOP_MULTIPLIER

    dynamic_stop = base_stop - atr_buffer

    # 最大止损距离限制
    max_stop_distance = buy_price * config.MAX_STOP_LOSS_PCT
    dynamic_stop = max(dynamic_stop, buy_price - max_stop_distance)

    # 止盈
    first_tp = T_weekly["MA20"]
    second_tp_defense = T_daily.get("MA10", T_daily["close"])

    # 风险收益比
    risk = buy_price - dynamic_stop
    reward = first_tp - buy_price if not pd.isna(first_tp) else 0

    if risk <= 0:
        rr_ratio = 0
    else:
        rr_ratio = reward / risk

    return {
        "buy_price": round(buy_price, 2),
        "prev_day_high": round(T1_daily["high"], 2),
        "dynamic_stop": round(dynamic_stop, 2),
        "stop_distance_pct": round((buy_price - dynamic_stop) / buy_price * 100, 2) if buy_price > 0 else 0,
        "first_tp": round(first_tp, 2) if not pd.isna(first_tp) else None,
        "second_tp_defense": round(second_tp_defense, 2),
        "risk_reward_ratio": round(rr_ratio, 2),
        "daily_ma5": round(T_daily.get("MA5", np.nan), 2),
        "daily_ma10": round(T_daily.get("MA10", np.nan), 2),
        "daily_ma20": round(T_daily.get("MA20", np.nan), 2),
        "rsi14": round(T_daily.get("RSI14", np.nan), 2),
    }


def calculate_risk_reward_score(rr_ratio: float, config: StrategyConfig) -> dict:
    """风险收益比评分"""
    if rr_ratio < 1.2:
        return {"pass": False, "score": 0, "details": {"reason": f"风险收益比过低:{rr_ratio:.2f}"}}

    if rr_ratio >= 2.5:
        score = 10
    elif rr_ratio >= 2.0:
        score = 8
    elif rr_ratio >= 1.5:
        score = 6
    elif rr_ratio >= 1.2:
        score = 3
    else:
        score = 0

    return {"pass": True, "score": score, "details": {"rr_ratio": rr_ratio}}


# ============================================================
# 综合评分与否决项
# ============================================================
def calculate_total_score(
    weekly_result: dict,
    fundamental_result: dict,
    daily_result: dict,
    rr_result: dict,
    market_env: dict,
    config: StrategyConfig,
) -> dict:
    """综合评分"""
    weekly_score = weekly_result["score"]
    fundamental_score = fundamental_result["score"]
    daily_score = daily_result["score"]
    rr_score = rr_result["score"]

    # 市场环境加减分
    env_bonus = 0
    env = market_env.get("env", "neutral")
    if env == "bull":
        env_bonus = 5
    elif env == "neutral":
        env_bonus = 0
    elif env == "bear_mild":
        env_bonus = -3

    # 市场宽度加减分
    breadth = market_env.get("breadth", None)
    breadth_bonus = 0
    if breadth is not None:
        if breadth > 0.30:
            breadth_bonus = 2  # 市场具备底部反弹条件
        elif breadth < 0.15:
            breadth_bonus = -3  # 极端熊市
    env_bonus += breadth_bonus

    total = weekly_score + daily_score + fundamental_score + rr_score + env_bonus

    # 分组门槛检查
    if weekly_score < config.WEEKLY_MIN_SCORE:
        return {"pass": False, "score": total, "reason": f"周线得分不足:{weekly_score}",
                "level": "LEFT_CANDIDATE"}
    if daily_score < config.DAILY_MIN_SCORE:
        return {"pass": False, "score": total, "reason": f"日线得分不足:{daily_score}",
                "level": "LEFT_CANDIDATE"}
    if fundamental_score < config.FUNDAMENTAL_MIN_SCORE:
        return {"pass": False, "score": total, "reason": f"基本面得分不足:{fundamental_score}",
                "level": "FUNDAMENTAL_PASS"}

    # 总分门槛
    min_score = config.MIN_SCORE
    if env == "bear_mild":
        min_score += 5

    if total < min_score:
        return {"pass": False, "score": total, "reason": f"总分不足:{total}<{min_score}",
                "level": "RIGHT_CONFIRMED"}

    # 信号等级
    rr_ratio = rr_result["details"].get("rr_ratio", 0)
    if rr_ratio >= 2.0 and total >= 80:
        level = "STRONG_BUY"
    elif rr_ratio >= config.MIN_RISK_REWARD and total >= config.MIN_SCORE:
        level = "BUY_SIGNAL"
    else:
        level = "RIGHT_CONFIRMED"

    # 信号强度分级
    if total >= 80:
        strength = "STRONG"
    elif total >= 70:
        strength = "MODERATE"
    elif total >= 60:
        strength = "WATCH"
    else:
        strength = "WEAK"

    return {
        "pass": True,
        "score": total,
        "level": level,
        "strength": strength,
        "weekly_score": weekly_score,
        "daily_score": daily_score,
        "fundamental_score": fundamental_score,
        "rr_score": rr_score,
        "env_bonus": env_bonus,
    }


def apply_veto_rules(stock_info: dict, config: StrategyConfig) -> Optional[str]:
    """
    否决项检查
    返回: None 表示通过，否则返回否决原因
    """
    week_return = stock_info.get("week_return", 0)
    weekly_turnover = stock_info.get("weekly_turnover", 0)
    roe = stock_info.get("roe", None)
    debt_ratio = stock_info.get("debt_ratio", None)
    normalized_drop = stock_info.get("normalized_drop", 0)
    close_above_ma5 = stock_info.get("close_above_ma5", True)
    rr_ratio = stock_info.get("rr_ratio", 999)

    if week_return < -0.15:
        return f"周跌幅超限:{week_return:.2%}"
    if weekly_turnover > 0.25:
        return f"周换手率超限:{weekly_turnover:.2%}"
    if roe is not None and roe < 0:
        return f"ROE为负:{roe:.1f}%"
    if debt_ratio is not None and debt_ratio > 80:
        return f"资产负债率超限:{debt_ratio:.1f}%"
    if normalized_drop > 3.0:
        return f"ATR归一化跌幅超限:{normalized_drop:.2f}"
    if not close_above_ma5:
        return "日线Close未站上MA5"
    if rr_ratio < 1.2:
        return f"风险收益比不足:{rr_ratio:.2f}"

    return None


# ============================================================
# 行业分散度控制
# ============================================================
def get_industry_info(codes: list) -> dict:
    """
    获取股票行业信息（多源降级）
    优化：对少量候选股票逐只查询所属行业，避免遍历全部行业板块
    """
    industry_map = {}

    # 方案1: AkShare 个股信息接口
    for code in codes:
        try:
            df_board = safe_api_call(
                ak.stock_individual_info_em,
                symbol=code,
                retries=2,
                retry_delay=3,
            )
            if df_board is not None and not df_board.empty:
                for _, row in df_board.iterrows():
                    item = str(row.iloc[0])
                    if "行业" in item:
                        industry_map[code] = str(row.iloc[1])
                        break
            time.sleep(0.5)
        except Exception:
            continue

    # 方案2: AkShare 板块接口批量查询
    missing_codes = [c for c in codes if c not in industry_map]
    if missing_codes:
        try:
            df_industry = ak.stock_board_industry_name_em()
            if df_industry is not None and not df_industry.empty:
                for _, row in df_industry.iterrows():
                    industry_name = row.get("板块名称", "")
                    if not industry_name:
                        continue
                    try:
                        df_cons = ak.stock_board_industry_cons_em(symbol=industry_name)
                        if df_cons is not None and not df_cons.empty:
                            code_col = "代码" if "代码" in df_cons.columns else df_cons.columns[0]
                            for c in df_cons[code_col]:
                                if str(c) in missing_codes:
                                    industry_map[str(c)] = industry_name
                    except Exception:
                        continue
                    if len(industry_map) >= len(codes):
                        break
        except Exception as e:
            logger.warning(f"AkShare 获取行业信息失败: {e}")

    # 方案3: BaoStock 行业分类
    missing_codes = [c for c in codes if c not in industry_map]
    if missing_codes and _BS_AVAILABLE:
        try:
            if BaoStockProvider.login():
                for code in missing_codes:
                    bs_code = BaoStockProvider._code_to_bs(code)
                    rs = bs.query_stock_industry(code=bs_code)
                    if rs.error_code == "0":
                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        if rows:
                            bs_df = pd.DataFrame(rows, columns=rs.fields)
                            if not bs_df.empty and "industry" in bs_df.columns:
                                industry_map[code] = bs_df["industry"].iloc[0]
        except Exception as e:
            logger.debug(f"BaoStock 获取行业信息失败: {e}")

    return industry_map


def apply_industry_diversification(
    results: list,
    config: StrategyConfig,
) -> list:
    """行业分散度控制"""
    if not results:
        return results

    # 尝试获取行业信息
    codes = [r["code"] for r in results]
    try:
        industry_map = get_industry_info(codes)
    except Exception:
        logger.warning("行业分散度控制跳过（无法获取行业数据）")
        for r in results:
            r["industry"] = "未知"
        return results

    # 填充行业信息
    for r in results:
        r["industry"] = industry_map.get(r["code"], "未知")

    # 按评分排序
    results.sort(key=lambda x: (-x["total_score"], -x.get("rr_ratio", 0)))

    # 行业限制
    industry_count = {}
    filtered = []
    for r in results:
        ind = r["industry"]
        if ind == "未知":
            filtered.append(r)
            continue
        count = industry_count.get(ind, 0)
        if count < config.MAX_SAME_INDUSTRY:
            filtered.append(r)
            industry_count[ind] = count + 1

    return filtered


# ============================================================
# 单只股票处理（含阶段计数返回）
# ============================================================
def process_stock(
    code: str,
    name: str,
    price: float,
    market_env: dict,
    config: StrategyConfig,
    cache: CacheManager,
) -> dict:
    """
    处理单只股票的完整流程
    返回: {"result": Optional[dict], "stage": str}
        stage: "data_fail" | "weekly_fail" | "fundamental_fail" |
               "daily_fail" | "rr_fail" | "veto_fail" | "score_fail" | "pass"
    """
    try:
        # === 获取周线数据 ===
        weekly_raw = get_weekly_data(code, config, cache)
        if weekly_raw is None or weekly_raw.empty:
            return {"result": None, "stage": "data_fail"}

        weekly_df = calculate_weekly_indicators(weekly_raw, config)
        if weekly_df.empty:
            return {"result": None, "stage": "data_fail"}

        # === 周线底部检查 ===
        weekly_result = check_weekly_bottom(weekly_df, config)
        if not weekly_result["pass"]:
            return {"result": None, "stage": "weekly_fail"}

        api_sleep(config)

        # === 基本面检查 ===
        fund_data = get_fundamental_data(code, config, cache)
        fundamental_result = check_fundamental(fund_data, config)
        if not fundamental_result["pass"]:
            return {"result": None, "stage": "fundamental_fail"}

        api_sleep(config)

        # === 日线数据 ===
        daily_raw = get_daily_data(code, config, cache)
        if daily_raw is None or daily_raw.empty:
            return {"result": None, "stage": "data_fail"}

        daily_df = calculate_daily_indicators(daily_raw)
        if daily_df.empty:
            return {"result": None, "stage": "data_fail"}

        # === 日线确认 ===
        daily_result = check_right_side_confirmation(daily_df, config)
        if not daily_result["pass"]:
            return {"result": None, "stage": "daily_fail"}

        # === 交易价位 ===
        trade_levels = calculate_trade_levels(weekly_df, daily_df, config)

        # === 风险收益比 ===
        rr_result = calculate_risk_reward_score(trade_levels["risk_reward_ratio"], config)
        if not rr_result["pass"]:
            return {"result": None, "stage": "rr_fail"}

        # === 否决项 ===
        T_weekly = weekly_df.iloc[-1]
        weekly_turnover_val = T_weekly.get("turnover", 0)
        if isinstance(weekly_turnover_val, (int, float)):
            if weekly_turnover_val > 1:
                weekly_turnover_val = weekly_turnover_val / 100.0
        else:
            weekly_turnover_val = 0

        veto_info = {
            "week_return": T_weekly["week_return"],
            "weekly_turnover": weekly_turnover_val,
            "roe": fundamental_result["details"].get("ROE", None),
            "debt_ratio": fundamental_result["details"].get("debt_ratio", None),
            "normalized_drop": weekly_result["details"].get("normalized_drop", 0),
            "close_above_ma5": True,  # 已在日线确认中检查
            "rr_ratio": trade_levels["risk_reward_ratio"],
        }
        veto_reason = apply_veto_rules(veto_info, config)
        if veto_reason is not None:
            return {"result": None, "stage": "veto_fail"}

        # === 综合评分 ===
        total_result = calculate_total_score(
            weekly_result, fundamental_result, daily_result, rr_result, market_env, config
        )
        if not total_result["pass"]:
            return {"result": None, "stage": "score_fail"}

        # === 组装结果 ===
        result = {
            "code": code,
            "name": name,
            "industry": "",
            "total_score": total_result["score"],
            "signal_level": total_result.get("level", ""),
            "market_env": market_env.get("env", "neutral"),
            "price": price,
            "weekly_ma20": round(T_weekly["MA20"], 2) if not pd.isna(T_weekly["MA20"]) else None,
            "week_return": round(T_weekly["week_return"] * 100, 2),
            "normalized_drop": round(weekly_result["details"].get("normalized_drop", 0), 2),
            "volume_ratio": round(T_weekly.get("volume_ratio", 0), 2),
            "turnover_ratio_val": round(T_weekly.get("turnover_ratio", 0), 2),
            "weekly_turnover": round(weekly_turnover_val * 100, 2),
            "volume_mode": weekly_result["details"].get("volume_mode", ""),
            "prev_20_low": round(T_weekly["prev_20_low"], 2) if not pd.isna(T_weekly["prev_20_low"]) else None,
            "week_low": round(T_weekly["low"], 2),
            "distance_to_low": round(
                (T_weekly["close"] / T_weekly["prev_20_low"] - 1) * 100, 2
            ) if not pd.isna(T_weekly["prev_20_low"]) and T_weekly["prev_20_low"] > 0 else None,
            "position_20": round(T_weekly["position_20"], 4) if not pd.isna(T_weekly["position_20"]) else None,
            "close_position": round(T_weekly["close_position"], 4) if not pd.isna(T_weekly["close_position"]) else None,
            "lower_shadow_ratio": round(T_weekly.get("lower_shadow_ratio", 0), 4),
            "dynamic_stop": trade_levels["dynamic_stop"],
            "stop_distance_pct": trade_levels["stop_distance_pct"],
            "buy_price": trade_levels["buy_price"],
            "prev_day_high": trade_levels["prev_day_high"],
            "daily_ma5": trade_levels["daily_ma5"],
            "daily_ma10": trade_levels["daily_ma10"],
            "daily_ma20": trade_levels["daily_ma20"],
            "rsi14": trade_levels["rsi14"],
            "daily_confirm_score": daily_result["score"],
            "first_tp": trade_levels["first_tp"],
            "second_tp_defense": trade_levels["second_tp_defense"],
            "rr_ratio": trade_levels["risk_reward_ratio"],
            "roe": fundamental_result["details"].get("ROE", None),
            "debt_ratio": fundamental_result["details"].get("debt_ratio", None),
            "cashflow": fundamental_result["details"].get("cashflow", None),
            "revenue_growth": fundamental_result["details"].get("revenue_growth", None),
            "profit_growth": fundamental_result["details"].get("profit_growth", None),
            # 分项得分
            "weekly_score": weekly_result["score"],
            "fundamental_score": fundamental_result["score"],
            "rr_score": rr_result["score"],
        }

        return {"result": result, "stage": "pass"}

    except Exception as e:
        logger.warning(f"{code} {name}: {e}")
        return {"result": None, "stage": "data_fail"}


# ============================================================
# 主函数
# ============================================================
def main():
    """策略主入口"""
    config = StrategyConfig()
    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    # ========== Step A: 市场环境评估 ==========
    market_env = get_market_environment(config, cache)

    if config.ENABLE_MARKET_FILTER and market_env["env"] == "bear_severe":
        logger.warning("=" * 60)
        logger.warning("当前市场环境不适合左侧策略，策略暂停！")
        logger.warning(f"市场状态: {market_env['description']}")
        logger.warning("=" * 60)
        return

    # ========== Step B & C: 获取并过滤股票列表 ==========
    stock_df = get_stock_list(config)
    stock_df = filter_stock_list(stock_df, config)

    if stock_df.empty:
        logger.info("基础过滤后无股票，策略结束")
        return

    # 提取代码、名称、价格
    col_code = "代码" if "代码" in stock_df.columns else "code"
    col_name = "名称" if "名称" in stock_df.columns else "name"
    col_price = "最新价" if "最新价" in stock_df.columns else "close"

    stocks = []
    for _, row in stock_df.iterrows():
        code = str(row[col_code]).zfill(6)
        name = str(row[col_name])
        try:
            price = float(row[col_price])
        except (ValueError, TypeError):
            continue
        stocks.append((code, name, price))

    # ========== 上市时间过滤 ==========
    col_list_date = None
    for col in ["上市时间", "上市日期", "list_date"]:
        if col in stock_df.columns:
            col_list_date = col
            break

    min_listing_date = datetime.now() - timedelta(days=config.MIN_LISTING_DAYS)
    filtered_stocks = []

    if col_list_date is not None:
        # 从股票列表中直接获取上市日期
        for code, name, price in stocks:
            matched = stock_df[stock_df[col_code].astype(str).str.zfill(6) == code]
            if matched.empty:
                matched = stock_df[stock_df[col_code].astype(str) == code]
            if not matched.empty:
                list_date_val = matched[col_list_date].iloc[0]
                try:
                    if pd.notna(list_date_val):
                        ld = pd.to_datetime(list_date_val)
                        if ld > min_listing_date:
                            continue  # 上市不足365天，跳过
                except Exception:
                    pass
            filtered_stocks.append((code, name, price))
        stocks = filtered_stocks
    else:
        # 股票列表无上市日期列，通过个股信息接口获取
        logger.info("股票列表无上市日期列，通过API逐只查询上市日期（抽样检查新股）...")
        filtered_stocks = []
        for code, name, price in stocks:
            # 通过代码规则快速判断：如果代码不是近几年上市的号段，直接通过
            # 对于无法判断的，调用API查询
            listing_date = get_listing_date(code, config)
            if listing_date is not None and listing_date > min_listing_date:
                continue  # 上市不足365天，跳过
            filtered_stocks.append((code, name, price))
            api_sleep(config)
        stocks = filtered_stocks

    logger.info(f"上市时间过滤后: {len(stocks)}")

    # ========== 市场宽度指标（可选） ==========
    breadth = calculate_market_breadth(stock_df, config, cache)
    if breadth is not None:
        market_env["breadth"] = breadth
        # 极端熊市额外判断
        if config.ENABLE_MARKET_FILTER and breadth < 0.15:
            logger.warning(f"市场宽度极低 ({breadth:.1%})，极端熊市环境，策略暂停！")
            return

    logger.info(f"待筛选股票数: {len(stocks)}")

    # ========== Step D~H: 逐只处理 ==========
    results = []
    weekly_pass_count = 0
    fundamental_pass_count = 0
    daily_pass_count = 0
    rr_pass_count = 0

    for i, (code, name, price) in enumerate(stocks):
        if (i + 1) % 100 == 0:
            logger.info(f"进度: {i + 1}/{len(stocks)}")

        stock_result = process_stock(code, name, price, market_env, config, cache)
        stage = stock_result["stage"]

        # 统计各阶段通过数
        if stage not in ("data_fail", "weekly_fail"):
            weekly_pass_count += 1
        if stage not in ("data_fail", "weekly_fail", "fundamental_fail"):
            fundamental_pass_count += 1
        if stage not in ("data_fail", "weekly_fail", "fundamental_fail", "daily_fail"):
            daily_pass_count += 1
        if stage not in ("data_fail", "weekly_fail", "fundamental_fail", "daily_fail", "rr_fail"):
            rr_pass_count += 1

        if stock_result["result"] is not None:
            results.append(stock_result["result"])

        api_sleep(config)

    # 输出各阶段统计
    logger.info(f"周线候选: {weekly_pass_count}")
    logger.info(f"基本面通过: {fundamental_pass_count}")
    logger.info(f"日线确认: {daily_pass_count}")
    logger.info(f"风险收益通过: {rr_pass_count}")
    logger.info(f"最终通过全部筛选: {len(results)}")

    if not results:
        logger.info("本次未筛选出符合条件的股票")
        return

    # ========== 行业分散度控制 ==========
    results = apply_industry_diversification(results, config)
    logger.info(f"行业分散后: {len(results)}")

    # ========== 排序与输出 ==========
    results.sort(key=lambda x: (-x["total_score"], -x["rr_ratio"]))

    # 构建输出 DataFrame（列名严格匹配规范）
    output_df = pd.DataFrame([{
        "代码": r["code"],
        "名称": r["name"],
        "所属行业": r["industry"],
        "综合评分": r["total_score"],
        "信号等级": r["signal_level"],
        "市场环境": r["market_env"],
        "现价": r["price"],
        "周线MA20": r["weekly_ma20"],
        "本周涨跌幅": r["week_return"],
        "ATR归一化跌幅": r["normalized_drop"],
        "周线成交量比": r["volume_ratio"],
        "周线换手比": r["turnover_ratio_val"],
        "本周换手率": r["weekly_turnover"],
        "量能模式": r["volume_mode"],
        "前20周低点": r["prev_20_low"],
        "本周最低价": r["week_low"],
        "距前低距离(%)": r["distance_to_low"],
        "20周价格位置": r["position_20"],
        "本周收盘位置": r["close_position"],
        "下影线比例": r["lower_shadow_ratio"],
        "动态止损价": r["dynamic_stop"],
        "止损距离(%)": r["stop_distance_pct"],
        "右侧买入价(日MA5)": r["buy_price"],
        "前一日最高价": r["prev_day_high"],
        "日MA5": r["daily_ma5"],
        "日MA10": r["daily_ma10"],
        "日MA20": r["daily_ma20"],
        "RSI14": r["rsi14"],
        "日线确认得分": r["daily_confirm_score"],
        "第一止盈价": r["first_tp"],
        "第二止盈防守线": r["second_tp_defense"],
        "风险收益比": r["rr_ratio"],
        "ROE": r["roe"],
        "资产负债率": r["debt_ratio"],
        "经营现金流": r["cashflow"],
        "营收同比": r["revenue_growth"],
        "净利润同比": r["profit_growth"],
    } for r in results])

    # 输出到 CSV
    output_file = "bottom_fishing_strategy_results.csv"
    output_df.to_csv(output_file, index=False, encoding="utf-8-sig")
    logger.info(f"结果已保存到: {output_file}")

    # 打印结果摘要
    logger.info("=" * 60)
    logger.info("选股结果摘要")
    logger.info("=" * 60)
    for _, row in output_df.iterrows():
        logger.info(
            f"  {row['代码']} {row['名称']} | "
            f"评分:{row['综合评分']} | "
            f"信号:{row['信号等级']} | "
            f"R/R:{row['风险收益比']} | "
            f"现价:{row['现价']} | "
            f"买入价:{row['右侧买入价(日MA5)']} | "
            f"止损:{row['动态止损价']}"
        )
    logger.info("=" * 60)
    logger.info(f"最终买入候选: {len(output_df)}")

    return output_df


# ============================================================
# 回测接口预留
# ============================================================
def generate_signal(date: str, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    """
    回测接口预留
    参数:
        date: 回测日期 YYYY-MM-DD
        config: 策略配置
    返回:
        信号 DataFrame 或 None

    前复权时变性说明：
        前复权价格会随新的除权除息重新计算历史值。
        实时选股模式使用当前前复权数据。
        历史回测如需严格一致性，应使用后复权或不复权 + 手动复权因子。
        AkShare免费接口不支持严格的历史财务数据时间回溯。

    回测成交规则：
        T日收盘后确认信号 → T+1日开盘买入
        成交价 = T+1 Open × (1 + SLIPPAGE)
    """
    logger.info(f"回测模式尚未实现，请求日期: {date}")
    return None


# ============================================================
# 推荐记录持久化与周度追踪回测模块
# ============================================================
# 数据文件路径（相对于项目根目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_HISTORY_FILE = str(_DATA_DIR / "signal_history.csv")
TRACKING_REPORT_FILE = str(_DATA_DIR / "tracking_report.csv")


def save_signal_history(output_df: pd.DataFrame) -> None:
    """
    保存本次推荐信号到历史记录文件
    每条记录包含推荐日期和推荐时收盘价作为基础锚点
    """
    if output_df is None or output_df.empty:
        return

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 构造信号记录
    records = []
    for _, row in output_df.iterrows():
        records.append({
            "推荐日期": today_str,
            "代码": row["代码"],
            "名称": row["名称"],
            "推荐时收盘价": row["现价"],
            "信号等级": row["信号等级"],
            "综合评分": row["综合评分"],
            "风险收益比": row["风险收益比"],
            "动态止损价": row["动态止损价"],
            "第一止盈价": row["第一止盈价"],
            "所属行业": row.get("所属行业", ""),
            "状态": "持仓跟踪",
        })

    new_df = pd.DataFrame(records)

    # 追加到历史文件
    history_path = Path(SIGNAL_HISTORY_FILE)
    if history_path.exists():
        existing = pd.read_csv(history_path, encoding="utf-8-sig")
        # 避免同一天同一股票重复记录
        existing_keys = set(
            existing["推荐日期"].astype(str) + "_" + existing["代码"].astype(str)
        )
        new_records = []
        for _, r in new_df.iterrows():
            key = f"{r['推荐日期']}_{r['代码']}"
            if key not in existing_keys:
                new_records.append(r)
        if new_records:
            append_df = pd.DataFrame(new_records)
            combined = pd.concat([existing, append_df], ignore_index=True)
            combined.to_csv(history_path, index=False, encoding="utf-8-sig")
            logger.info(f"新增 {len(new_records)} 条推荐记录到 {SIGNAL_HISTORY_FILE}")
        else:
            logger.info("无新增推荐记录（已存在）")
    else:
        new_df.to_csv(history_path, index=False, encoding="utf-8-sig")
        logger.info(f"创建推荐历史文件，保存 {len(new_df)} 条记录到 {SIGNAL_HISTORY_FILE}")


def load_signal_history() -> Optional[pd.DataFrame]:
    """加载历史推荐记录"""
    history_path = Path(SIGNAL_HISTORY_FILE)
    if not history_path.exists():
        logger.info("无历史推荐记录文件")
        return None
    try:
        df = pd.read_csv(history_path, encoding="utf-8-sig")
        if df.empty:
            return None
        df["推荐日期"] = pd.to_datetime(df["推荐日期"])
        return df
    except Exception as e:
        logger.warning(f"加载历史记录失败: {e}")
        return None


def get_current_price(code: str, config: StrategyConfig) -> Optional[float]:
    """获取股票当前价格（多源降级）"""
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df = fetch_stock_history(
            code=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq", config=config
        )
        if df is not None and not df.empty:
            df = normalize_columns(df)
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return None


def weekly_performance_review(config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    """
    每周追踪回测：统计所有历史推荐股票的表现
    基础锚点：推荐日的收盘价
    统计指标：
        - 当前价格
        - 绝对收益率（相对推荐日收盘价）
        - 最大回撤（如可获取期间数据）
        - 是否触发止损
        - 是否达到第一止盈
        - 持仓天数
        - 状态（持仓/止损/止盈）
    """
    if config is None:
        config = StrategyConfig()

    history = load_signal_history()
    if history is None:
        logger.info("无历史推荐记录，跳过周度追踪")
        return None

    logger.info("=" * 60)
    logger.info("周度追踪回测报告")
    logger.info("=" * 60)
    logger.info(f"历史推荐总数: {len(history)}")

    today = datetime.now()
    tracking_results = []

    for idx, row in history.iterrows():
        code = str(row["代码"]).zfill(6)
        name = row["名称"]
        rec_date = row["推荐日期"]
        rec_price = float(row["推荐时收盘价"])
        stop_price = float(row["动态止损价"]) if pd.notna(row.get("动态止损价")) else None
        tp_price = float(row["第一止盈价"]) if pd.notna(row.get("第一止盈价")) else None
        signal_level = row.get("信号等级", "")
        score = row.get("综合评分", 0)

        holding_days = (today - rec_date).days

        # 获取推荐日至今的日线数据（多源降级）
        try:
            start_date = rec_date.strftime("%Y%m%d")
            end_date = today.strftime("%Y%m%d")
            df = fetch_stock_history(
                code=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq", config=config
            )
            api_sleep(config)
        except Exception:
            df = None

        if df is None or df.empty:
            tracking_results.append({
                "代码": code,
                "名称": name,
                "推荐日期": rec_date.strftime("%Y-%m-%d"),
                "推荐时收盘价": rec_price,
                "当前价格": None,
                "收益率(%)": None,
                "持仓天数": holding_days,
                "期间最高价": None,
                "期间最低价": None,
                "最大浮盈(%)": None,
                "最大回撤(%)": None,
                "是否触发止损": "数据缺失",
                "是否达止盈": "数据缺失",
                "当前状态": "数据缺失",
                "信号等级": signal_level,
                "综合评分": score,
            })
            continue

        df = normalize_columns(df)
        if "close" not in df.columns:
            continue

        current_price = float(df["close"].iloc[-1])
        period_high = float(df["high"].max()) if "high" in df.columns else current_price
        period_low = float(df["low"].min()) if "low" in df.columns else current_price

        # 收益率
        return_pct = (current_price / rec_price - 1) * 100

        # 最大浮盈
        max_profit_pct = (period_high / rec_price - 1) * 100

        # 最大回撤（从推荐价计算）
        max_drawdown_pct = (period_low / rec_price - 1) * 100

        # 是否触发止损
        hit_stop = False
        if stop_price is not None and period_low <= stop_price:
            hit_stop = True

        # 是否达到止盈
        hit_tp = False
        if tp_price is not None and period_high >= tp_price:
            hit_tp = True

        # 判断当前状态
        if hit_stop and not hit_tp:
            status = "已止损"
        elif hit_tp:
            status = "已止盈"
        elif return_pct > 0:
            status = "浮盈持仓"
        elif return_pct > -5:
            status = "小幅浮亏"
        else:
            status = "较大浮亏"

        # 检查是否先止盈后回落（通过日线时序判断）
        if hit_tp and hit_stop:
            # 需要判断哪个先发生
            if "high" in df.columns and "low" in df.columns:
                for _, d_row in df.iterrows():
                    if tp_price is not None and d_row["high"] >= tp_price:
                        status = "已止盈"
                        break
                    if stop_price is not None and d_row["low"] <= stop_price:
                        status = "已止损"
                        break

        tracking_results.append({
            "代码": code,
            "名称": name,
            "推荐日期": rec_date.strftime("%Y-%m-%d"),
            "推荐时收盘价": round(rec_price, 2),
            "当前价格": round(current_price, 2),
            "收益率(%)": round(return_pct, 2),
            "持仓天数": holding_days,
            "期间最高价": round(period_high, 2),
            "期间最低价": round(period_low, 2),
            "最大浮盈(%)": round(max_profit_pct, 2),
            "最大回撤(%)": round(max_drawdown_pct, 2),
            "是否触发止损": "是" if hit_stop else "否",
            "是否达止盈": "是" if hit_tp else "否",
            "当前状态": status,
            "信号等级": signal_level,
            "综合评分": score,
        })

    if not tracking_results:
        logger.info("无有效追踪记录")
        return None

    report_df = pd.DataFrame(tracking_results)

    # 统计汇总
    valid_returns = report_df["收益率(%)"].dropna()
    if len(valid_returns) > 0:
        avg_return = valid_returns.mean()
        win_count = (valid_returns > 0).sum()
        loss_count = (valid_returns <= 0).sum()
        win_rate = win_count / len(valid_returns) * 100
        max_win = valid_returns.max()
        max_loss = valid_returns.min()
        median_return = valid_returns.median()
    else:
        avg_return = win_rate = max_win = max_loss = median_return = 0
        win_count = loss_count = 0

    # 按状态统计
    status_counts = report_df["当前状态"].value_counts()

    logger.info(f"追踪股票数: {len(report_df)}")
    logger.info(f"有效数据数: {len(valid_returns)}")
    logger.info("-" * 40)
    logger.info(f"平均收益率: {avg_return:.2f}%")
    logger.info(f"中位数收益: {median_return:.2f}%")
    logger.info(f"胜率: {win_rate:.1f}% ({win_count}胜/{loss_count}负)")
    logger.info(f"最大盈利: {max_win:.2f}%")
    logger.info(f"最大亏损: {max_loss:.2f}%")
    logger.info("-" * 40)
    logger.info("状态分布:")
    for status, count in status_counts.items():
        logger.info(f"  {status}: {count}")
    logger.info("-" * 40)

    # 按推荐周分组统计
    report_df["推荐周"] = pd.to_datetime(report_df["推荐日期"]).dt.isocalendar().week
    report_df["推荐年"] = pd.to_datetime(report_df["推荐日期"]).dt.year

    logger.info("按推荐批次统计:")
    grouped = report_df.groupby(["推荐年", "推荐周"])
    for (year, week), group in grouped:
        grp_returns = group["收益率(%)"].dropna()
        if len(grp_returns) > 0:
            grp_avg = grp_returns.mean()
            grp_win_rate = (grp_returns > 0).sum() / len(grp_returns) * 100
            logger.info(
                f"  {year}年第{week}周: "
                f"{len(group)}只 | "
                f"平均收益:{grp_avg:.2f}% | "
                f"胜率:{grp_win_rate:.0f}%"
            )

    # 输出每只股票明细
    logger.info("=" * 60)
    logger.info("个股追踪明细:")
    logger.info("=" * 60)
    for _, r in report_df.iterrows():
        ret_str = f"{r['收益率(%)']:.1f}%" if pd.notna(r['收益率(%)']) else "N/A"
        logger.info(
            f"  {r['代码']} {r['名称']} | "
            f"推荐:{r['推荐日期']} | "
            f"锚点:{r['推荐时收盘价']} | "
            f"现价:{r['当前价格']} | "
            f"收益:{ret_str} | "
            f"{r['当前状态']}"
        )

    # 保存报告
    # 移除临时列
    output_report = report_df.drop(columns=["推荐周", "推荐年"], errors="ignore")
    output_report.to_csv(TRACKING_REPORT_FILE, index=False, encoding="utf-8-sig")
    logger.info(f"\n追踪报告已保存到: {TRACKING_REPORT_FILE}")

    return output_report


def update_signal_status(report_df: pd.DataFrame) -> None:
    """
    根据追踪结果更新历史记录的状态字段
    已止损/已止盈的记录标记为结束，不再追踪
    """
    history_path = Path(SIGNAL_HISTORY_FILE)
    if not history_path.exists() or report_df is None or report_df.empty:
        return

    try:
        history = pd.read_csv(history_path, encoding="utf-8-sig")
        history["推荐日期"] = pd.to_datetime(history["推荐日期"]).dt.strftime("%Y-%m-%d")

        for _, row in report_df.iterrows():
            status = row["当前状态"]
            if status in ("已止损", "已止盈"):
                mask = (
                    (history["代码"].astype(str).str.zfill(6) == str(row["代码"]).zfill(6)) &
                    (history["推荐日期"] == row["推荐日期"])
                )
                history.loc[mask, "状态"] = status

        history.to_csv(history_path, index=False, encoding="utf-8-sig")
    except Exception as e:
        logger.warning(f"更新信号状态失败: {e}")


# ============================================================
# 主入口增强：支持选股 + 追踪双模式
# ============================================================
def run_strategy():
    """
    完整策略运行入口：
    1. 运行选股策略
    2. 保存推荐信号
    3. 执行周度追踪回测
    """
    config = StrategyConfig()

    try:
        # Step 1: 运行选股
        logger.info("【阶段1】运行选股策略...")
        output_df = main()

        # Step 2: 保存推荐信号（如有新信号）
        if output_df is not None and not output_df.empty:
            save_signal_history(output_df)

        # Step 3: 执行周度追踪回测
        logger.info("")
        logger.info("【阶段2】执行周度追踪回测...")
        report = weekly_performance_review(config)

        # Step 4: 更新状态
        if report is not None:
            update_signal_status(report)

        logger.info("")
        logger.info("策略运行完成")
    finally:
        # 确保 BaoStock 退出登录
        if _BS_AVAILABLE:
            BaoStockProvider.logout()


def run_tracking_only():
    """
    仅运行追踪回测（不执行选股）
    适用于周末或盘后复盘
    """
    config = StrategyConfig()
    try:
        logger.info("=" * 60)
        logger.info("周度追踪回测模式（仅追踪，不选股）")
        logger.info("=" * 60)

        report = weekly_performance_review(config)
        if report is not None:
            update_signal_status(report)

        return report
    finally:
        if _BS_AVAILABLE:
            BaoStockProvider.logout()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode == "track":
            # 仅追踪模式: python bottom_fishing_strategy.py track
            run_tracking_only()
        elif mode == "full":
            # 完整模式: python bottom_fishing_strategy.py full
            run_strategy()
        elif mode == "screen":
            # 仅选股模式: python bottom_fishing_strategy.py screen
            try:
                output = main()
                if output is not None and not output.empty:
                    save_signal_history(output)
            finally:
                if _BS_AVAILABLE:
                    BaoStockProvider.logout()
        else:
            logger.info(f"未知模式: {mode}")
            logger.info("用法: python bottom_fishing_strategy.py [screen|track|full]")
            logger.info("  screen - 仅选股并保存推荐记录")
            logger.info("  track  - 仅追踪历史推荐的表现")
            logger.info("  full   - 选股 + 追踪（完整流程）")
    else:
        # 默认：完整模式
        run_strategy()
