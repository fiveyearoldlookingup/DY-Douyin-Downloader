"""下载器：将视频和图片下载到本地，并记录到 SQLite 数据库。

支持:
- 视频下载（.mp4）
- 图片集下载（.jpg / .webp）
- 自动跳过已存在的文件
- 网络波动自动重试
- SQLite 持久化记录（替代 metadata.json）
- 断点续爬状态机（pending → downloading → success/failed）
- 多线程并行下载（Phase 3）
"""

import concurrent.futures
import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from typing import Callable

import requests

from .user import Aweme
from .utils import sanitize_filename, ensure_dir

logger = logging.getLogger(__name__)


class Downloader:
    """媒体文件下载器。

    使用 SQLite 数据库替代 metadata.json 进行去重和状态追踪。

    用法:
        from douyin.database import DatabaseManager
        db = DatabaseManager()
        downloader = Downloader(base_dir="./downloads", subfolder="username", db=db)
        downloader.download_aweme(aweme)
    """

    def __init__(
        self,
        base_dir: str = "./downloads",
        subfolder: str = "",
        skip_existing: bool = True,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        db=None,  # DatabaseManager | None
        max_workers: int = 5,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
        progress_callback: Callable[[int, int], None] | None = None,
    ):
        """
        Args:
            base_dir: 下载根目录
            subfolder: 子目录（用于按用户分组，如用户名）
            skip_existing: 是否跳过已存在的文件
            max_retries: 下载失败最多重试次数
            retry_delay: 重试间隔秒数
            db: DatabaseManager 实例（None 则回退到 metadata.json）
            max_workers: 并行下载线程数（默认 5）
            min_delay: 请求间最小延迟（秒）
            max_delay: 请求间最大延迟（秒）
            progress_callback: 进度回调 (completed_files, total_files)
        """
        self.base_dir = base_dir
        self.subfolder = subfolder
        if subfolder:
            safe_sub = sanitize_filename(subfolder)
            base_dir = os.path.join(base_dir, safe_sub)
        self.video_dir = os.path.join(base_dir, "videos")
        self.image_dir = os.path.join(base_dir, "images")
        self.skip_existing = skip_existing
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.progress_callback = progress_callback
        ensure_dir(self.video_dir)
        ensure_dir(self.image_dir)

        # ── 数据库 ──
        self.db = db

        # ── 元数据（回退用）──
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.metadata = self._load_metadata()

        # ── 并行下载 ──
        self.max_workers = max_workers
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._rate_lock = threading.Lock()
        self._last_request_time: float = 0.0

        # 启动时标记崩溃残留
        if self.db:
            stale = self.db.mark_stale_downloads()
            if stale:
                logger.info(f"标记了 {stale} 个崩溃残留帖子为 failed")

    # ── 上下文管理 ──────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        """关闭线程池和数据库连接。"""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self.db:
            self.db.close()

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="dy-dl-",
            )
        return self._executor

    # ── 元数据管理（回退用）─────────────────────────────────

    def _load_metadata(self) -> dict:
        """加载已有 metadata.json（向后兼容）。"""
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "posts" in data:
                        return data
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "username": self.subfolder or "",
            "posts": {},
            "total_posts": 0,
            "total_files": 0,
        }

    def _save_metadata(self):
        """保存元数据到 JSON 文件（向后兼容）。"""
        self.metadata["total_posts"] = len(self.metadata["posts"])
        self.metadata["total_files"] = sum(
            len(p.get("files", [])) for p in self.metadata["posts"].values()
        )
        try:
            with open(self.metadata_path, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def is_aweme_downloaded(self, aweme_id: str) -> bool:
        """检查帖子是否已下载过（先用 DB，回退到 metadata.json）。"""
        aweme_id = str(aweme_id)
        if self.db:
            post = self.db.get_post_by_aweme_id(aweme_id)
            return post is not None and post.get("status") == "success"
        return aweme_id in self.metadata.get("posts", {})

    def get_metadata(self) -> dict:
        """返回当前元数据（只读）。"""
        return dict(self.metadata)

    # ── 文件名生成 ─────────────────────────────────────────

    @staticmethod
    def _make_filename(aweme: Aweme, index: int, total: int, ext: str) -> str:
        """生成优化后的文件名。

        格式: {YYYYMMDD}_{desc截断40字}_{aweme_id后8位}[_{序号}]{ext}
        示例: 20260613_清晨阳光正好_2937048320_1.jpg
        """
        date_str = (
            datetime.fromtimestamp(aweme.create_time).strftime("%Y%m%d")
            if aweme.create_time
            else datetime.now().strftime("%Y%m%d")
        )

        safe_desc = (
            sanitize_filename(aweme.desc, max_length=40)
            if aweme.desc
            else "untitled"
        )

        short_id = str(aweme.aweme_id)[-8:]

        if total > 1:
            return f"{date_str}_{safe_desc}_{short_id}_{index + 1}{ext}"
        else:
            return f"{date_str}_{safe_desc}_{short_id}{ext}"

    # ── 文件下载 ────────────────────────────────────────────

    def _download_file(self, url: str, filepath: str) -> tuple[bool, str]:
        """下载单个文件到指定路径，失败自动重试。

        Returns:
            (成功标志, 状态消息)
        """
        if self.skip_existing and os.path.exists(filepath):
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            return True, f"⏭ 跳过（已存在）: {os.path.basename(filepath)} ({size_mb:.1f} MB)"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, headers=headers, stream=True, timeout=60)
                resp.raise_for_status()

                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

                file_size = os.path.getsize(filepath)
                size_mb = file_size / (1024 * 1024)
                tag = "✓" if attempt == 1 else f"✓ (重试{attempt})"
                msg = f"{tag} 下载完成: {os.path.basename(filepath)} ({size_mb:.1f} MB)"
                return True, msg

            except requests.RequestException as e:
                if os.path.exists(filepath):
                    os.remove(filepath)

                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
                else:
                    msg = f"✗ 下载失败 (已重试{self.max_retries}次): {os.path.basename(filepath)} — {e}"
                    return False, msg

        return False, ""

    def _rate_limited_download(self, url: str, filepath: str) -> tuple[bool, str]:
        """带速率限制的文件下载（多线程共享锁）。"""
        with self._rate_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_delay:
                delay = random.uniform(
                    max(0, self.min_delay - elapsed),
                    max(0, self.max_delay - elapsed),
                )
                time.sleep(delay)
            self._last_request_time = time.time()

        return self._download_file(url, filepath)

    # ── 主入口 ──────────────────────────────────────────────

    def download_aweme(self, aweme: Aweme) -> list[str]:
        """下载一个帖子的所有媒体文件，并记录到数据库。

        Phase 2 状态机: pending → downloading → success/failed

        Args:
            aweme: 帖子对象

        Returns:
            成功下载的文件路径列表
        """
        aweme_id = str(aweme.aweme_id)

        if not aweme.media_items:
            return []

        # 检查是否已下载
        if self.is_aweme_downloaded(aweme_id):
            # 从 DB 或 metadata 返回已下载的文件路径
            if self.db:
                post = self.db.get_post_by_aweme_id(aweme_id)
                if post:
                    files = self.db.get_files_by_post_id(post["id"])
                    return [f["file_path"] for f in files]
            existing = self.metadata.get("posts", {}).get(aweme_id, {}).get("files", [])
            target_dir = self.image_dir if aweme.is_image_post else self.video_dir
            return [os.path.join(target_dir, f) for f in existing]

        # 防止并发重复下载
        if self.db and not self.db.acquire_download_slot(aweme_id):
            return []

        # 更新状态为 downloading
        if self.db:
            # 确保帖子记录存在
            self._ensure_post_in_db(aweme)
            self.db.update_post_status(aweme_id, "downloading")

        # 确定下载目录
        target_dir = self.video_dir if aweme.is_video else self.image_dir

        total = len(aweme.media_items)
        file_infos = []  # (url, filepath, ext)

        for i, media in enumerate(aweme.media_items):
            filename = self._make_filename(aweme, i, total, media.file_extension)
            filepath = os.path.join(target_dir, filename)
            file_infos.append((media.url, filepath, media.file_extension))

        # ── 多线程并行下载 ──
        downloaded = []
        failed = []
        executor = self._get_executor()

        futures = {
            executor.submit(self._rate_limited_download, url, fp): (url, fp)
            for url, fp, _ in file_infos
        }

        for future in concurrent.futures.as_completed(futures):
            ok, msg = future.result()
            _, filepath = futures[future]
            if ok:
                downloaded.append(filepath)
            else:
                failed.append(filepath)
            if msg:
                print(f"  {msg}")
                if self.progress_callback:
                    self.progress_callback(
                        len(downloaded) + len(failed), total
                    )

        # ── 记录到数据库 ──
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        if self.db:
            post = self.db.get_post_by_aweme_id(aweme_id)
            post_id = post["id"] if post else None

            if post_id and downloaded:
                for fp in downloaded:
                    fname = os.path.basename(fp)
                    ext = os.path.splitext(fname)[1].lower()
                    file_type = "image" if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else "video"
                    file_size = os.path.getsize(fp) if os.path.exists(fp) else 0
                    rel_path = os.path.relpath(fp, self.base_dir)
                    self.db.insert_file({
                        "post_id": post_id,
                        "file_path": rel_path,
                        "file_size": file_size,
                        "file_type": file_type,
                        "url": "",
                    })

            # 更新状态
            if post_id:
                new_status = "success" if downloaded else "failed"
                self.db.update_post_status(aweme_id, new_status, now if downloaded else None)

            self.db.release_download_slot(aweme_id)
        else:
            # 回退到 metadata.json
            if downloaded or total > 0:
                self.metadata.setdefault("posts", {})
                self.metadata["posts"][aweme_id] = {
                    "aweme_id": aweme_id,
                    "desc": aweme.desc,
                    "create_time": aweme.create_time,
                    "aweme_type": aweme.aweme_type,
                    "files": [os.path.basename(f) for f in downloaded],
                    "downloaded_at": now,
                }
                if self.subfolder:
                    self.metadata["username"] = self.subfolder
                self._save_metadata()

        return downloaded

    def _ensure_post_in_db(self, aweme: Aweme) -> int | None:
        """确保帖子记录存在于数据库中，不存在则创建。"""
        if not self.db:
            return None

        post = self.db.get_post_by_aweme_id(str(aweme.aweme_id))
        if post:
            return post["id"]

        return self.db.insert_post({
            "aweme_id": str(aweme.aweme_id),
            "user_id": None,  # 后续由 caller 更新
            "aweme_type": aweme.aweme_type,
            "desc": aweme.desc,
            "create_time": aweme.create_time,
            "duration": 0,
            "cover_url": "",
            "raw_json": "",
        })

    def set_user_id_for_aweme(self, aweme_id: str, user_id: int) -> None:
        """关联帖子到用户。"""
        if self.db:
            self.db._execute(
                "UPDATE posts SET user_id = ? WHERE aweme_id = ?",
                (user_id, aweme_id),
            )
            self.db.get_connection().commit()
