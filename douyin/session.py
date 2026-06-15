"""抖音 API 会话管理。

纯 Python 实现：使用 requests + aBogus 签名直接调用抖音 Web API，
无需 Playwright 浏览器。签名算法来自 TikTokDownloader 项目。

核心流程:
1. 构建 API 参数（设备信息、用户 ID、游标等）
2. 用 ABogus 对 URL 参数做签名
3. 发送 GET 请求到 aweme/v1/web/aweme/post/
4. 解析响应，逐页爬取直到 has_more=0

用法:
    from douyin.session import DouyinSession

    session = DouyinSession()
    posts, user_info = session.get_user_posts(sec_user_id, max_pages=3)
    detail = session.get_post_detail(aweme_id)
"""

import time
import random
import logging
from urllib.parse import quote, urlencode

import requests

from .anti_crawl import UARotator, ProxyPool, CookiePool
from .encrypt import ABogus, generate_fake_ms_token, USERAGENT

logger = logging.getLogger(__name__)

# ── API 端点 ──
API_POST_LIST = "https://www.douyin.com/aweme/v1/web/aweme/post/"
API_DETAIL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# ── 基础请求参数（模拟真实 Chrome 浏览器环境） ──
BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Mac OS",
    "support_h265": "1",
    "support_dash": "1",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": "1470",
    "screen_height": "956",
    "browser_language": "zh-CN",
    "browser_platform": "MacIntel",
    "browser_name": "Chrome",
    "browser_version": "149.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "149.0.0.0",
    "os_name": "Mac OS",
    "os_version": "10.15.7",
    "cpu_core_num": "10",
    "device_memory": "16",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
    "uifid": "",
}


class DouyinSession:
    """抖音 API 会话 — 用 aBogus 签名发请求，无需浏览器。

    特性:
    - 自动生成 aBogus 签名
    - 自动生成 msToken 随机令牌
    - UA/代理/Cookie 池轮换（反爬增强）
    - 请求间隔随机延迟，模拟人类行为
    - 连接复用（requests.Session）
    - 自动分页爬取全部帖子

    用法:
        session = DouyinSession()
        posts, user_info = session.get_user_posts(sec_user_id)
    """

    def __init__(
        self,
        user_agent: str = USERAGENT,
        timeout: int = 15,
        proxy: str | None = None,
        cookie: str = "",
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        # Phase 6: 反爬池
        ua_rotator: UARotator | None = None,
        proxy_pool: ProxyPool | None = None,
        cookie_pool: CookiePool | None = None,
    ):
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_delay = max_delay

        # ── 反爬池 ──
        self._ua_rotator = ua_rotator or UARotator()
        self._proxy_pool = proxy_pool or ProxyPool(
            [proxy] if proxy else []
        )
        self._cookie_pool = cookie_pool or CookiePool(
            [cookie] if cookie else []
        )

        # 初始 UA（第一个请求会切换到池中的下一个）
        self.user_agent = user_agent

        # 签名生成器
        self._ab = ABogus(user_agent=user_agent)

        # HTTP 会话
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        })
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

        # 设置浏览器 cookie
        if cookie:
            self._set_cookie_string(cookie)

        # 应用到 session
        self._apply_rotation()

    # ═══════════════════════════════════════════════════════════
    # 反爬轮换
    # ═══════════════════════════════════════════════════════════

    def _apply_rotation(self) -> None:
        """切换到池中的下一个 UA / 代理 / Cookie。"""
        # UA 轮换
        ua = self._ua_rotator.next()
        self.user_agent = ua
        self._session.headers.update({"User-Agent": ua})

        # 代理轮换
        proxy = self._proxy_pool.next()
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        else:
            self._session.proxies = {}

        # Cookie 轮换
        cookie = self._cookie_pool.next()
        if cookie:
            self._set_cookie_string(cookie)

    def _handle_response_error(self, resp) -> bool:
        """检测反爬信号。返回 True 表示应重试并切换身份。

        - 403: Cookie 可能被标记
        - 空响应体: 代理可能被限
        """
        if resp.status_code == 403:
            current_cookie = self._cookie_pool.peek()
            if current_cookie:
                self._cookie_pool.mark_bad(current_cookie)
            self._apply_rotation()
            return True

        if resp.status_code == 200 and len(resp.content) < 100:
            # 空响应可能是代理问题
            current_proxy = self._session.proxies.get("http", "")
            if current_proxy:
                self._proxy_pool.fail(current_proxy)
            self._apply_rotation()
            return True

        return False

    # ═══════════════════════════════════════════════════════════
    # 公开 API
    # ═══════════════════════════════════════════════════════════

    def get_user_posts(
        self,
        sec_user_id: str,
        max_pages: int | None = None,
        count: int = 18,
    ) -> tuple[list[dict], dict | None]:
        """获取用户全部公开帖子。

        逐页调用 aweme/v1/web/aweme/post/，自动处理分页，
        直到 has_more=0 或达到页数上限。

        Args:
            sec_user_id: 用户 base64 ID（从 URL 提取）
            max_pages: 最大页数限制，None=不限制
            count: 每页帖子数（默认 18，对应抖音默认值）

        Returns:
            (posts_list, user_info_dict)
            - posts_list: 所有帖子的原始 aweme 数据列表
            - user_info_dict: 包含 user 字段的响应数据（仅第一页有）
        """
        cursor = 0
        page_num = 0
        all_posts: list[dict] = []
        user_info: dict | None = None
        seen_cursors: set[int] = set()

        while True:
            page_num += 1
            data = self._fetch_post_page(sec_user_id, cursor, count)

            if data is None:
                logger.warning(f"第 {page_num} 页请求失败")
                break

            aweme_list = data.get("aweme_list", [])
            cursor = data.get("max_cursor", 0)
            has_more = data.get("has_more", 0)

            if not aweme_list:
                logger.info("aweme_list 为空，爬取结束")
                break

            if cursor in seen_cursors:
                logger.info("游标重复，爬取结束")
                break
            seen_cursors.add(cursor)

            # 第一页响应包含用户信息
            if user_info is None and "user" in data:
                user_info = data

            all_posts.extend(aweme_list)
            print(f"📄 第 {page_num} 页: {len(aweme_list)} 条帖子, has_more={has_more}")

            if max_pages and page_num >= max_pages:
                print(f"⏹ 达到页数限制 {max_pages}")
                break
            if not has_more:
                print("✅ 已获取全部帖子")
                break

        print(f"📊 共收集 {len(all_posts)} 个帖子")
        return all_posts, user_info

    def get_post_detail(self, aweme_id: str) -> dict | None:
        """获取单个帖子详情。

        调用 aweme/v1/web/aweme/detail/ 获取 aweme 的完整信息，
        包含视频播放地址、图片地址等。

        Args:
            aweme_id: 帖子 ID（modal_id 或 aweme_id）

        Returns:
            aweme_detail 字典，失败返回 None
        """
        params = dict(BASE_PARAMS)
        params.update({
            "aweme_id": aweme_id,
            "version_code": "190500",
            "version_name": "19.5.0",
            "msToken": generate_fake_ms_token(),
        })

        url = self._build_signed_url(API_DETAIL, params)
        print(f"🔍 获取帖子详情: {aweme_id}")

        try:
            resp = self._session.get(
                url,
                headers={"Referer": f"https://www.douyin.com/video/{aweme_id}"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("aweme_detail", data)
        except Exception as e:
            logger.error(f"获取帖子详情失败: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _fetch_post_page(
        self,
        sec_user_id: str,
        max_cursor: int = 0,
        count: int = 18,
    ) -> dict | None:
        """获取单页帖子列表。

        Args:
            sec_user_id: 用户 base64 ID
            max_cursor: 分页游标，0=第一页
            count: 每页数量

        Returns:
            API 响应 JSON 字典，失败或 Geo 封锁返回 None
        """
        params = dict(BASE_PARAMS)
        params.update({
            "sec_user_id": sec_user_id,
            "max_cursor": max_cursor,
            "locate_query": "false",
            "show_live_replay_strategy": "1",
            "need_time_list": "1",
            "time_list_query": "0",
            "whale_cut_token": "",
            "cut_version": "1",
            "count": count,
            "publish_video_strategy_type": "2",
            "msToken": generate_fake_ms_token(),
        })

        url = self._build_signed_url(API_POST_LIST, params)
        referer = f"https://www.douyin.com/user/{sec_user_id}"

        try:
            resp = self._session.get(
                url,
                headers={"Referer": referer},
                timeout=self.timeout,
            )
            resp.raise_for_status()

            # 检测 Geo-IP 封锁：状态码 200 但响应体为空
            if not resp.content:
                logger.warning(
                    "API 返回空响应 — 可能被 Geo-IP 封锁。"
                    "抖音 aweme/post/ 接口仅限中国大陆 IP 访问。"
                    "请使用 proxy 参数配置中国代理，例如：\n"
                    '    DouyinSession(proxy="http://your-china-proxy:port")'
                )
                return None

            return resp.json()
        except requests.RequestException as e:
            logger.error(f"请求帖子列表失败: {e}")
            return None
        except ValueError as e:
            logger.error(f"解析 JSON 失败: {e}")
            return None

    def _build_signed_url(self, api_url: str, params: dict) -> str:
        """构建带签名的完整 URL。

        流程:
        1. 将 params 字典 URL 编码
        2. 用 ABogus 生成 a_bogus 签名
        3. 拼接到 URL

        Args:
            api_url: API 端点 URL
            params: 请求参数字典

        Returns:
            完整的签名 URL，如: https://.../post/?...&a_bogus=XXX
        """
        # URL 编码参数（safe="=" 保留 = 号不被编码）
        params_str = urlencode(params, safe="=", quote_via=quote)

        # 生成 a_bogus 签名
        a_bogus = self._ab.get_value(params_str, "GET")

        return f"{api_url}?{params_str}&a_bogus={a_bogus}"

    def _wait(self):
        """请求间随机延迟，模拟人类浏览行为。

        使用对数正态分布模拟更自然的延迟模式。
        """
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _set_cookie_string(self, cookie_str: str):
        """解析 cookie 字符串并注入到会话。

        支持两种格式：
        - 标准: "name1=value1; name2=value2"
        - JSON: '{"name1": "value1", "name2": "value2"}'
        """
        import json as _json

        try:
            # 尝试 JSON 格式
            cookie_dict = _json.loads(cookie_str)
            if isinstance(cookie_dict, dict):
                for name, value in cookie_dict.items():
                    self._session.cookies.set(name, value, domain=".douyin.com")
                return
        except (_json.JSONDecodeError, ValueError):
            pass

        # 标准 cookie 字符串格式
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                self._session.cookies.set(name.strip(), value.strip(), domain=".douyin.com")

    def close(self):
        """关闭 HTTP 会话。"""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
