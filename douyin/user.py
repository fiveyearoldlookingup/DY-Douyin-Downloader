"""用户主页爬取。

从 API 响应中解析用户信息和帖子数据，提取可下载的媒体 URL。
"""

from dataclasses import dataclass, field


@dataclass
class UserInfo:
    """用户基本信息。"""

    nickname: str = ""  # 昵称
    unique_id: str = ""  # 抖音号
    signature: str = ""  # 个人简介
    avatar_url: str = ""  # 头像 URL
    follower_count: int = 0  # 粉丝数
    following_count: int = 0  # 关注数
    aweme_count: int = 0  # 作品数


@dataclass
class MediaItem:
    """单个媒体文件（视频或图片）。"""

    url: str
    file_extension: str = ".mp4"


@dataclass
class Aweme:
    """一个抖音帖子（视频或图文）。"""

    aweme_id: str
    aweme_type: int  # 0=视频, 2=图片集, 4=直播
    desc: str  # 描述文字
    create_time: int  # Unix 时间戳
    media_items: list[MediaItem] = field(default_factory=list)
    raw_data: dict | None = None  # 原始 API 数据

    @property
    def is_video(self) -> bool:
        return self.aweme_type == 0

    @property
    def is_image_post(self) -> bool:
        return self.aweme_type == 2

    @property
    def is_live(self) -> bool:
        return self.aweme_type == 4


def parse_user_info(data: dict) -> UserInfo:
    """从 API 响应中解析用户信息。"""
    user = data.get("user", {})
    return UserInfo(
        nickname=user.get("nickname", ""),
        unique_id=user.get("unique_id", ""),
        signature=user.get("signature", ""),
        avatar_url=user.get("avatar_medium", {}).get("url_list", [""])[0],
        follower_count=user.get("follower_count", 0),
        following_count=user.get("following_count", 0),
        aweme_count=user.get("aweme_count", 0),
    )


def parse_aweme(raw: dict) -> Aweme:
    """从 API 返回的单个帖子数据中提取关键字段。

    支持的帖子类型：
    - aweme_type=0: 普通视频 → video.play_addr.url_list
    - aweme_type=2: 静态图片集 → images[].url_list
    - aweme_type=4: 直播 → 无可下载媒体
    - aweme_type=68: 带音乐幻灯片/图集 → images[].url_list
    """
    aweme_id = raw.get("aweme_id", "")
    aweme_type = raw.get("aweme_type", 0)
    desc = raw.get("desc", "")
    create_time = raw.get("create_time", 0)

    media_items = []

    # ── 图片类帖子（type=2 静态图集 / type=68 音乐幻灯片）──
    if aweme_type in (2, 68):
        images = raw.get("images") or raw.get("image_post_info", {}).get("images", [])
        for img in images:
            # url_list = 无水印低质量，download_url_list = 带水印高清
            # 优先拿无水印版本，选最后一项（通常为 .jpeg 格式）
            url_list = img.get("url_list", [])
            if url_list:
                best_url = url_list[-1]
                # 根据 URL 确定扩展名
                ext = ".jpg"
                if ".webp" in best_url.split("?")[0]:
                    ext = ".webp"
                elif ".png" in best_url.split("?")[0]:
                    ext = ".png"
                media_items.append(MediaItem(url=best_url, file_extension=ext))

    # ── 视频类帖子 ──
    elif aweme_type == 0:
        video = raw.get("video", {})
        # 优先 h264 编码，兼容性最好
        play_addr = video.get("play_addr_h264") or video.get("play_addr") or {}
        url_list = play_addr.get("url_list", [])
        if url_list:
            media_items.append(MediaItem(url=url_list[0], file_extension=".mp4"))

    # aweme_type=4 是直播，无可下载媒体，跳过

    return Aweme(
        aweme_id=str(aweme_id),
        aweme_type=aweme_type,
        desc=desc,
        create_time=create_time,
        media_items=media_items,
        raw_data=raw,
    )


def parse_aweme_list(raw_list: list[dict]) -> list[Aweme]:
    """解析原始帖子列表。"""
    return [parse_aweme(item) for item in raw_list if item.get("aweme_id")]
