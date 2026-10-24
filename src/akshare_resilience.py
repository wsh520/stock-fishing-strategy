#!/usr/bin/env python3
"""AkShare 通道韧性层：连接复用 + 强制超时 + 通道熔断 + 有限重试。

## 为什么需要这一层（run #111/#112/#113 实证）

AkShare 的每个接口内部都是 `requests.get(url, ..., timeout=None)`：

1. **每次调用新建 Session** → 每只股票一次 TCP/TLS 握手。全市场 3000 只就是 3000 次
   重连；本机实测同一批 8 只股票：原始实现 1.63 秒/次，复用连接后 0.31 秒/次（约 5×）。
2. **timeout 缺省为 None** → 服务端不响应时该线程可以**无限期**卡在 recv 上。
   run #111 的 600511 就是在这种状态下卡了 1750 秒以上，直到整条 job 被取消；
   `socket.setdefaulttimeout(20)` 对 requests 无效（urllib3 会显式把 socket 超时设为 None）。
3. **没有通道级熔断** → 东财（push2his.eastmoney.com）抖动时，每只股票都要先重试
   `MAX_RETRY` 次再走新浪兜底，3000 只股票 = 9000 次无效请求，日志里就是「一直重连」。

## 本层做的五件事（全部幂等、只影响本进程）

1. 线程内复用 keep-alive Session（连接池），把每只股票的握手成本摊掉；
2. 调用方未指定 timeout 时注入默认值（`AK_HTTP_TIMEOUT_SEC`，默认 20 秒），
   把「无限挂起」变成「可重试的失败」；
3. 通道熔断：连续失败达阈值（默认 3 次）后冷却（默认 120 秒），期间**直接跳过**该通道，
   由上层走备用通道（东财 → 新浪），不再对每只股票重试同一批死连接；
4. 熔断冷却**逐级升级** + 真实错误可见（2026-10-24，run #116~#118 实证）：
   东财在 GitHub Actions 云 IP 上基本不可达（同代码本地 0.5 秒成功 ⇒ IP 级封锁），
   每次运行熔断 14~16 次、每 120 秒重试一次死通道。升级规则：第 N 次熔断冷却
   = 基础冷却 × ESCALATE^(N-1)，封顶 MAX_SEC，通道恢复成功即清零；
   同时把最近一次失败的类型与消息写进熔断 WARNING（此前只有 DEBUG，CI 日志
   看不到东财到底怎么失败的），让下一次运行自诊断；
5. 单通道有限重试（默认 3 次，指数退避 + 抖动，参数与 `FETCH_RETRY_*` 同源）。

不改变任何**选股口径**：只影响「同一接口请求怎么发、失败后怎么办」。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("akshare_http")

# 与 StrategyConfig.AK_HTTP_TIMEOUT_SEC 默认值一致；configure() 会在每次运行开始时覆盖。
DEFAULT_TIMEOUT_SEC = 20.0
DEFAULT_CHANNEL_FAIL_THRESHOLD = 3
DEFAULT_CHANNEL_COOLDOWN_SEC = 120.0
# 熔断冷却升级（2026-10-24）：1.0 = 不升级（旧行为），由 StrategyConfig.
# AK_CHANNEL_COOLDOWN_ESCALATE 覆盖；None/0 = 不封顶。
DEFAULT_CHANNEL_COOLDOWN_ESCALATE = 1.0
DEFAULT_CHANNEL_COOLDOWN_MAX_SEC = None

_timeout = {"sec": DEFAULT_TIMEOUT_SEC}
_install_lock = threading.Lock()
_installed = False
_originals: dict[str, Any] = {}
_tls = threading.local()

# 通道名：eastmoney（东财日线/财报）、sina（新浪日线/指数/财务指标）
_EASTMONEY = "eastmoney"
_SINA = "sina"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class ChannelUnavailable(RuntimeError):
    """通道熔断中：调用方应立即换备用通道，而不是重试。"""


class _Channel:
    __slots__ = ("name", "failures", "open_until", "ok", "failed", "skipped", "trips", "last_error")

    def __init__(self, name: str):
        self.name = name
        self.failures = 0          # 连续失败次数（成功即清零）
        self.open_until = 0.0      # 熔断截止时间戳
        self.ok = 0
        self.failed = 0
        self.skipped = 0
        self.trips = 0             # 本次运行内熔断次数（成功即清零；用于冷却升级）
        self.last_error = None     # 最近一次失败原因（随熔断 WARNING 输出，便于 CI 诊断）


_channels: dict[str, _Channel] = {}
_channels_lock = threading.Lock()


def channel(name: str) -> _Channel:
    with _channels_lock:
        state = _channels.get(name)
        if state is None:
            state = _channels[name] = _Channel(name)
        return state


# ===========================================================================
# HTTP 层：连接复用 + 默认超时
# ===========================================================================

def configure(timeout_sec: Optional[float] = None) -> None:
    """设置默认 HTTP 超时（秒）。已安装的补丁会读取该值，无需重装。"""
    if timeout_sec is not None and float(timeout_sec) > 0:
        _timeout["sec"] = float(timeout_sec)


def _make_session():
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    # 东财/新浪对无 UA 的请求会返回 403 或空体；补一个常规浏览器 UA。
    session.headers.setdefault("User-Agent", _UA)
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0, pool_block=False)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _session() -> Any:
    session = getattr(_tls, "session", None)
    if session is None:
        session = _make_session()
        _tls.session = session
    return session


def _inject_timeout(kwargs: dict) -> dict:
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = _timeout["sec"]
    return kwargs


def install(timeout_sec: Optional[float] = None) -> bool:
    """幂等安装 HTTP 补丁；返回 True 表示本次真正打了补丁。

    只补 `requests`（AkShare 1.18 的全部目标接口都走它）；装了 curl_cffi 的版本一并补。
    任何异常都吞掉并降级为「未安装」——数据源不能因为韧性层装不上而不可用。
    """
    global _installed
    configure(timeout_sec)
    with _install_lock:
        if _installed:
            return False
        try:
            import requests
            import requests.api
            import requests.sessions
        except Exception as exc:  # pragma: no cover - requests 是 akshare 的硬依赖
            logger.warning("AkShare 韧性层未安装（requests 不可用）: %s", exc)
            return False

        original_session_request = requests.sessions.Session.request
        original_api_request = requests.api.request

        def _session_request(self, method, url, *args, **kwargs):
            if args:  # 位置参数（含极少数把 timeout 传成位置参数的老代码）原样透传
                return original_session_request(self, method, url, *args, **kwargs)
            return original_session_request(self, method, url, **_inject_timeout(kwargs))

        def _api_request(method, url, **kwargs):
            # AkShare 走的是 requests.get(...) → requests.api.request(...)，
            # 这里换成线程内复用的 Session，保留 keep-alive 连接。
            return _session().request(method, url, **_inject_timeout(kwargs))

        requests.sessions.Session.request = _session_request
        requests.api.request = _api_request
        _originals["session_request"] = original_session_request
        _originals["api_request"] = original_api_request
        _installed = True

        # 新版 AkShare 的部分接口改用 curl_cffi；存在则同样注入超时并复用会话。
        try:
            import curl_cffi.requests as curl_requests  # type: ignore

            original_curl_session_request = curl_requests.Session.request

            def _curl_session_request(self, method, url, **kwargs):
                return original_curl_session_request(self, method, url, **_inject_timeout(kwargs))

            curl_requests.Session.request = _curl_session_request
            _originals["curl_session_request"] = original_curl_session_request
        except Exception:  # pragma: no cover - 可选依赖
            pass

        logger.debug("AkShare 韧性层已安装：连接复用 + 默认超时 %.0f 秒", _timeout["sec"])
        return True


def uninstall() -> None:
    """撤销补丁（测试用）。生产进程不需要调用。"""
    global _installed
    with _install_lock:
        if not _installed:
            return
        import requests
        import requests.api
        import requests.sessions

        requests.sessions.Session.request = _originals["session_request"]
        requests.api.request = _originals["api_request"]
        if "curl_session_request" in _originals:
            try:
                import curl_cffi.requests as curl_requests  # type: ignore
                curl_requests.Session.request = _originals["curl_session_request"]
            except Exception:  # pragma: no cover
                pass
        _originals.clear()
        _tls.session = None
        _installed = False


def is_installed() -> bool:
    return _installed


# ===========================================================================
# 通道熔断
# ===========================================================================

def is_open(name: str) -> bool:
    return channel(name).open_until > time.time()


def record_success(name: str) -> None:
    state = channel(name)
    state.failures = 0
    state.open_until = 0.0
    state.trips = 0  # 通道恢复 ⇒ 熔断升级清零，下次故障从基础冷却重新开始
    state.ok += 1


def _escalated_cooldown(trips: int, base: float, escalate: float,
                        max_cooldown: Optional[float]) -> float:
    """第 N 次熔断的冷却时长：base × escalate^(N-1)，封顶 max_cooldown。

    trips<=1 或 escalate<=1 时恒等于 base（向后兼容旧行为）。
    """
    if trips <= 1 or escalate <= 1.0:
        return base
    grown = base * (escalate ** (trips - 1))
    if max_cooldown is not None and max_cooldown > 0:
        grown = min(float(max_cooldown), grown)
    return grown


def record_failure(name: str, threshold: int = DEFAULT_CHANNEL_FAIL_THRESHOLD,
                   cooldown_sec: float = DEFAULT_CHANNEL_COOLDOWN_SEC,
                   escalate: float = DEFAULT_CHANNEL_COOLDOWN_ESCALATE,
                   max_cooldown_sec: Optional[float] = DEFAULT_CHANNEL_COOLDOWN_MAX_SEC,
                   error: Optional[BaseException] = None) -> None:
    state = channel(name)
    state.failures += 1
    state.failed += 1
    if error is not None:
        state.last_error = f"{type(error).__name__}: {str(error)[:160]}"
    threshold = max(1, int(threshold))
    if state.failures >= threshold:
        first_time = state.open_until <= time.time()
        if first_time:
            state.trips += 1
        effective = _escalated_cooldown(state.trips, max(0.0, float(cooldown_sec)),
                                        float(escalate or 1.0), max_cooldown_sec)
        state.open_until = time.time() + effective
        if first_time:
            # 错误类型+消息随 WARNING 输出：CI 日志只看 WARNING 以上，
            # 没有它就无法区分「超时/403/连接重置」，下次运行仍是盲修。
            detail = f"；最近错误: {state.last_error}" if state.last_error else ""
            logger.warning("AkShare 通道 %s 第 %d 次熔断（累计失败 %d 次），冷却 %.0f 秒："
                           "期间跳过该通道改用备用通道%s",
                           name, state.trips, state.failures, effective, detail)


def reset_channels() -> None:
    """清空通道状态（每次选股运行开始时调用，避免跨运行污染）。"""
    with _channels_lock:
        _channels.clear()


def snapshot() -> dict[str, dict[str, Any]]:
    """通道状态快照，供日志留档（ok/failed/skipped/trips/熔断剩余秒数/最近错误）。"""
    now = time.time()
    with _channels_lock:
        items = list(_channels.items())
    return {
        name: {
            "ok": st.ok,
            "failed": st.failed,
            "skipped": st.skipped,
            "failures": st.failures,
            "trips": st.trips,
            "open_for_sec": round(max(0.0, st.open_until - now), 1),
            "last_error": st.last_error,
        }
        for name, st in items
    }


def format_summary() -> str:
    snap = snapshot()
    if not snap:
        return "AkShare 通道: 未使用"
    parts = []
    for name, st in sorted(snap.items()):
        piece = f"{name} 成功{st['ok']}/失败{st['failed']}/跳过{st['skipped']}"
        if st.get("trips"):
            piece += f"/熔断{st['trips']}次"
        if st["open_for_sec"] > 0:
            piece += f"（熔断剩余{st['open_for_sec']:.0f}秒）"
        if st.get("last_error"):
            piece += f"（最近错误: {st['last_error']}）"
        parts.append(piece)
    return "AkShare 通道: " + "，".join(parts)


# ===========================================================================
# 有限重试 + 熔断感知的调用入口
# ===========================================================================

def _backoff_seconds(attempt: int, config: Any = None) -> float:
    """与 screening_engine._retry_backoff_seconds 同源的指数退避 + 抖动。"""
    base = float(getattr(config, "FETCH_RETRY_BACKOFF_BASE_SEC", 0.5) if config is not None else 0.5)
    cap = float(getattr(config, "FETCH_RETRY_BACKOFF_MAX_SEC", 8.0) if config is not None else 8.0)
    jitter = float(getattr(config, "FETCH_RETRY_JITTER", 0.3) if config is not None else 0.3)
    return min(cap, base * (2 ** attempt)) * random.uniform(1.0 - jitter, 1.0 + jitter)


def _is_empty(raw: Any) -> bool:
    if raw is None:
        return True
    try:
        return bool(getattr(raw, "empty"))
    except Exception:
        return False


def call(channel_name: str, fetcher: Callable[[], Any], label: str,
         retries: int = 2, config: Any = None, retry_on_empty: bool = False) -> Any:
    """在指定通道上执行一次 AkShare 调用，带熔断感知的有限重试。

    返回原始结果；通道熔断中或重试耗尽返回 None（调用方据此走备用通道）。
    通道熔断中**不重试**：对已经判定为死的通道再打 3 次是纯粹的时间浪费，
    也是日志里「一直重连」的来源。
    """
    state = channel(channel_name)
    if state.open_until > time.time():
        state.skipped += 1
        raise ChannelUnavailable(f"{label}: 通道 {channel_name} 熔断中")

    threshold = int(getattr(config, "AK_CHANNEL_FAIL_THRESHOLD", DEFAULT_CHANNEL_FAIL_THRESHOLD)
                    or DEFAULT_CHANNEL_FAIL_THRESHOLD)
    cooldown = float(getattr(config, "AK_CHANNEL_COOLDOWN_SEC", DEFAULT_CHANNEL_COOLDOWN_SEC)
                     or DEFAULT_CHANNEL_COOLDOWN_SEC)
    escalate = float(getattr(config, "AK_CHANNEL_COOLDOWN_ESCALATE", DEFAULT_CHANNEL_COOLDOWN_ESCALATE)
                     or DEFAULT_CHANNEL_COOLDOWN_ESCALATE)
    raw_max = getattr(config, "AK_CHANNEL_COOLDOWN_MAX_SEC", DEFAULT_CHANNEL_COOLDOWN_MAX_SEC)
    max_cooldown = float(raw_max) if raw_max else None
    attempts = max(0, int(retries)) + 1
    last_error: Optional[BaseException] = None

    for attempt in range(attempts):
        try:
            raw = fetcher()
            if _is_empty(raw) and not retry_on_empty:
                # 空结果是合法返回（如新股无历史），不算通道故障。
                record_success(channel_name)
                return raw
            if _is_empty(raw):
                last_error = RuntimeError(f"{label}: 空返回")
            else:
                record_success(channel_name)
                return raw
        except ChannelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 数据源异常一律按失败重试
            last_error = exc
        if attempt < attempts - 1:
            time.sleep(_backoff_seconds(attempt, config))

    record_failure(channel_name, threshold, cooldown, escalate=escalate,
                   max_cooldown_sec=max_cooldown, error=last_error)
    logger.debug("%s 获取失败（通道 %s，已尝试 %d 次）: %s", label, channel_name, attempts, last_error)
    return None


def has_channel(channel_name: str) -> bool:
    """是否使用过某通道（用于日志判断该不该打印）。"""
    with _channels_lock:
        return channel_name in _channels
