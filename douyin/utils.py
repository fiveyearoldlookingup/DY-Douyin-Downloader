"""工具函数：URL 解析、文件名清理等。"""

import re
import os
from urllib.parse import urlparse, parse_qs


def extract_sec_user_id(url: str) -> str:
    """从抖音用户主页 URL 中提取 sec_user_id。

    示例:
        https://www.douyin.com/user/MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU
        → MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # 路径格式: /user/<sec_user_id>
    parts = path.split("/")
    if "user" in parts:
        idx = parts.index("user")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    raise ValueError(f"无法从 URL 提取 sec_user_id: {url}")


def extract_aweme_id(url: str) -> str:
    """从抖音帖子 URL 中提取 aweme_id (modal_id)。

    示例:
        https://www.douyin.com/user/MS4wLjAB...?modal_id=7524544482937048320
        → 7524544482937048320
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    modal_id = params.get("modal_id", [None])[0]
    if modal_id:
        return modal_id
    raise ValueError(f"无法从 URL 提取 aweme_id (modal_id): {url}")


def sanitize_filename(text: str, max_length: int = 80) -> str:
    """清理文本使其可用作文件名。

    - 移除或替换非法字符
    - 截断到指定长度
    """
    if not text:
        return "untitled"
    # 移除换行和多余空白
    text = re.sub(r"\s+", " ", text).strip()
    # 替换文件系统不允许的字符
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    # 截断
    if len(text) > max_length:
        text = text[:max_length]
    return text.strip()


def extract_web_rid(url_or_id: str) -> str:
    """从直播 URL 或纯 web_rid 中提取直播间短 ID。

    示例:
        https://live.douyin.com/123456789 → 123456789
        https://www.douyin.com/follow/live/723565127698 → 723565127698
        123456789 → 123456789
    """
    import re

    s = url_or_id.strip()

    # 纯数字 web_rid，直接返回
    if s.isdigit() and len(s) < 20:
        return s

    parsed = urlparse(s)
    path = parsed.path.rstrip("/")
    path_lower = path.lower()

    # 已知直播路径模式
    if ("/live/" in path_lower or "live.douyin.com" in (parsed.netloc or "")):
        parts = path.split("/")
        # 取最后一个纯数字段
        for part in reversed(parts):
            if part.isdigit():
                return part

    # 尝试从路径中提取最后一段数字
    m = re.search(r"/(\d{6,19})(?:\?|$)", path)
    if m:
        return m.group(1)

    # 兜底：已经是纯 web_rid
    if s.isdigit():
        return s

    raise ValueError(f"无法从 URL 提取 web_rid: {url_or_id}")


def get_download_path(base_dir: str, aweme_type: int) -> str:
    """根据帖子类型返回下载子目录。"""
    if aweme_type == 2:
        return os.path.join(base_dir, "downloads", "images")
    else:
        return os.path.join(base_dir, "downloads", "videos")


def ensure_dir(path: str) -> None:
    """确保目录存在。"""
    os.makedirs(path, exist_ok=True)
