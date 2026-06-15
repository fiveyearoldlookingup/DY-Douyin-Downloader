"""反爬增强模块：User-Agent 轮换、代理池、Cookie 池。

通过轮换请求身份降低被抖音反爬系统封禁的风险。

用法:
    from douyin.anti_crawl import UARotator, ProxyPool, CookiePool

    ua_rotator = UARotator()
    proxy_pool = ProxyPool(["http://p1:7890", "http://p2:7890"])
    cookie_pool = CookiePool(["sessionid=aaa;...", "sessionid=bbb;..."])
"""

import random
import threading
import time

# ═══════════════════════════════════════════════════════════════
# User-Agent 池
# ═══════════════════════════════════════════════════════════════

# 10 个常见浏览器 UA，覆盖 Chrome/Safari/Firefox/Edge
USER_AGENTS = [
    # Chrome 149 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    # Chrome 149 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    # Chrome 149 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    # Safari 18.5 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
    # Firefox 136 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Firefox 136 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Firefox 136 Linux
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Edge 148 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    # Edge 148 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    # Chrome 150 macOS (最新)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
]


class UARotator:
    """User-Agent 轮换器。"""

    def __init__(self, user_agents: list[str] | None = None):
        self._agents = user_agents or USER_AGENTS.copy()
        self._lock = threading.Lock()
        self._index = 0

    def next(self) -> str:
        """获取下一个 UA（轮询模式）。"""
        with self._lock:
            ua = self._agents[self._index]
            self._index = (self._index + 1) % len(self._agents)
            return ua

    def random(self) -> str:
        """随机获取一个 UA。"""
        return random.choice(self._agents)

    @property
    def count(self) -> int:
        return len(self._agents)


# ═══════════════════════════════════════════════════════════════
# 代理池
# ═══════════════════════════════════════════════════════════════

class ProxyPool:
    """代理池：轮询 + 自动故障切换。

    失败的代理会被暂时加入黑名单，5 分钟后自动恢复。
    """

    def __init__(self, proxies: list[str] | None = None):
        self._lock = threading.Lock()
        self._proxies: list[str] = list(proxies) if proxies else []
        self._index = 0
        self._blacklist: dict[str, float] = {}  # proxy -> expire_time
        self._blacklist_duration = 300.0  # 5 分钟

    def next(self) -> str | None:
        """获取下一个可用代理。无可用代理返回 None。"""
        with self._lock:
            now = time.time()
            # 清理过期黑名单
            expired = [p for p, t in self._blacklist.items() if t < now]
            for p in expired:
                del self._blacklist[p]

            available = [p for p in self._proxies if p not in self._blacklist]
            if not available:
                return None

            proxy = available[self._index % len(available)]
            self._index = (self._index + 1) % len(available)
            return proxy

    def fail(self, proxy: str) -> None:
        """标记代理失败，暂时拉黑。"""
        with self._lock:
            self._blacklist[proxy] = time.time() + self._blacklist_duration

    def add(self, proxy: str) -> None:
        """添加代理。"""
        with self._lock:
            if proxy not in self._proxies:
                self._proxies.append(proxy)

    def remove(self, proxy: str) -> None:
        """移除代理。"""
        with self._lock:
            if proxy in self._proxies:
                self._proxies.remove(proxy)

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        with self._lock:
            now = time.time()
            return sum(1 for p in self._proxies
                       if p not in self._blacklist or self._blacklist[p] < now)


# ═══════════════════════════════════════════════════════════════
# Cookie 池
# ═══════════════════════════════════════════════════════════════

class CookiePool:
    """Cookie 池：轮询 + 低质量 cookie 降权。

    标记为 bad 的 cookie 会被移到队列末尾。
    """

    def __init__(self, cookies: list[str] | None = None):
        self._lock = threading.Lock()
        self._cookies: list[str] = list(cookies) if cookies else []
        self._index = 0

    def next(self) -> str:
        """获取下一个 Cookie。"""
        with self._lock:
            if not self._cookies:
                return ""
            cookie = self._cookies[self._index]
            self._index = (self._index + 1) % len(self._cookies)
            return cookie

    def peek(self) -> str:
        """查看当前 Cookie（不推进索引）。"""
        with self._lock:
            if not self._cookies:
                return ""
            return self._cookies[self._index]

    def mark_bad(self, cookie: str) -> None:
        """标记 Cookie 为低质量，移到末尾。"""
        with self._lock:
            if cookie in self._cookies:
                self._cookies.remove(cookie)
                self._cookies.append(cookie)

    def add(self, cookie: str) -> None:
        """添加 Cookie。"""
        with self._lock:
            if cookie not in self._cookies:
                self._cookies.append(cookie)

    @property
    def count(self) -> int:
        return len(self._cookies)

    @property
    def cookies(self) -> list[str]:
        return list(self._cookies)
