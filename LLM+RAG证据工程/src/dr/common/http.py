"""HTTP工具函数

统一的HTTP请求重试逻辑，避免在多个脚本中重复。
"""
import os
import socket
import time
import requests
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Force IPv4: some Docker/VM environments have broken IPv6, causing
# "Network is unreachable" even though IPv4 works fine.
# Set FORCE_IPV4=1 (or auto-detect) to monkey-patch socket.getaddrinfo.
# ---------------------------------------------------------------------------
def _patch_ipv4_only():
    """Filter out IPv6 addresses from DNS resolution."""
    _orig = socket.getaddrinfo

    def _ipv4_only(*args, **kwargs):
        responses = _orig(*args, **kwargs)
        filtered = [r for r in responses if r[0] == socket.AF_INET]
        return filtered if filtered else responses  # fallback to original if no IPv4

    socket.getaddrinfo = _ipv4_only
    logger.info("HTTP: forced IPv4-only DNS resolution")

if os.getenv("FORCE_IPV4", "1") == "1":
    _patch_ipv4_only()

# 默认超时（可被覆盖）
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_SLEEP = 2.0


def request_with_retries(
    method: str,
    url: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_sleep: float = DEFAULT_RETRY_SLEEP,
    **kwargs
) -> requests.Response:
    """健壮的HTTP请求（带重试和指数退避）

    这是DR管道中所有HTTP请求的统一入口，确保：
    1. 自动重试（默认4次）
    2. 指数退避（避免服务器过载）
    3. 参数隔离（避免跨重试污染）
    4. 详细日志（方便调试）

    Args:
        method: HTTP方法（GET/POST/PUT/DELETE）
        url: 目标URL
        max_retries: 最大重试次数（默认4）
        retry_sleep: 基础重试延迟（秒，默认2.0）
        **kwargs: requests.request的其他参数（timeout, json, params等）

    Returns:
        requests.Response对象（已调用raise_for_status）

    Raises:
        RuntimeError: 重试耗尽后仍失败

    Example:
        >>> from dr.config import Config
        >>> r = request_with_retries(
        ...     "GET",
        ...     "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        ...     params={"db": "pubmed", "term": "aspirin"},
        ...     timeout=Config.PUBMED_TIMEOUT
        ... )
        >>> data = r.json()

    Notes:
        - trust_env参数：Ollama需要trust_env=False避免代理问题
        - timeout参数：每次重试都会重新应用（不会累积）
        - 指数退避：第1次失败等2s，第2次4s，第3次6s，第4次8s
    """
    last_exception = None
    timeout_default = kwargs.get("timeout", DEFAULT_TIMEOUT)
    trust_env_default = kwargs.get("trust_env", True)

    for attempt in range(1, max_retries + 1):
        try:
            # 复制kwargs以避免污染原始参数
            kw = dict(kwargs)
            timeout = kw.pop("timeout", timeout_default)
            trust_env = kw.pop("trust_env", trust_env_default)

            # 使用Session对象设置trust_env（trust_env是Session属性，不是request参数）
            sess = requests.Session()
            sess.trust_env = trust_env
            r = sess.request(method, url, timeout=timeout, **kw)

            r.raise_for_status()
            return r

        except requests.exceptions.RequestException as e:
            last_exception = e
            logger.warning(
                "HTTP %s %s failed on attempt %d/%d: %s",
                method, url, attempt, max_retries, e
            )

            if attempt < max_retries:
                sleep_time = retry_sleep * attempt  # 指数退避
                logger.debug("Retrying in %.1f seconds...", sleep_time)
                time.sleep(sleep_time)

    # 重试耗尽
    raise RuntimeError(
        f"HTTP {method} {url} failed after {max_retries} retries: {last_exception}"
    )


def polite_request(
    method: str,
    url: str,
    delay: float = 0.6,
    **kwargs
) -> requests.Response:
    """礼貌的HTTP请求（自动延迟，适用于NCBI等公共API）

    NCBI E-utilities要求：
    - 无API key：最多3次/秒（延迟0.34s）
    - 有API key：最多10次/秒（延迟0.1s）

    Args:
        method: HTTP方法
        url: 目标URL
        delay: 请求后延迟（秒，默认0.6s）
        **kwargs: 传递给request_with_retries

    Returns:
        requests.Response对象

    Example:
        >>> r = polite_request(
        ...     "GET",
        ...     "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        ...     params={"db": "pubmed", "term": "aspirin"},
        ...     delay=0.6
        ... )
    """
    try:
        response = request_with_retries(method, url, **kwargs)
        return response
    finally:
        # 无论成功或失败，都延迟（避免频繁重试导致封IP）
        time.sleep(delay)
