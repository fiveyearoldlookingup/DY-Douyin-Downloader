"""后台定时同步调度器。

通过 APScheduler 定时检查已订阅创作者的新帖子并自动下载。
类似 RSS 订阅模式。

用法:
    from douyin.scheduler import SyncScheduler
    scheduler = SyncScheduler(db, download_dir, session_factory)
    scheduler.start(interval_hours=6)
    # ... 应用运行中 ...
    scheduler.stop()
"""

import logging
import time
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class SyncScheduler:
    """订阅同步后台调度器。

    特性:
    - 定时检查所有已启用的订阅
    - 首次启动立即执行一次同步
    - 仅下载 last_aweme_id 之后的新帖
    - 支持手动触发同步
    """

    def __init__(
        self,
        db,  # DatabaseManager
        download_dir: str,
        session_factory: Callable,  # () -> DouyinSession
        max_workers: int = 3,
    ):
        self.db = db
        self.download_dir = download_dir
        self._session_factory = session_factory
        self.max_workers = max_workers
        self._scheduler: BackgroundScheduler | None = None
        self._running = False

        # 进度报告（供 WebUI 使用）
        self._progress_callbacks: list[Callable] = []

    def start(self, interval_hours: int = 6) -> None:
        """启动调度器。

        Args:
            interval_hours: 同步间隔（小时），默认 6 小时
        """
        if self._running:
            return

        self._scheduler = BackgroundScheduler(daemon=True)

        # 定期任务
        self._scheduler.add_job(
            self._sync_all,
            IntervalTrigger(hours=interval_hours),
            id="sync_all_periodic",
            name="定期同步所有订阅",
            replace_existing=True,
        )

        # 启动后 10 秒执行首次同步
        self._scheduler.add_job(
            self._sync_all,
            trigger="date",
            run_date=None,  # 立即
            id="sync_all_initial",
            name="首次同步",
        )

        self._scheduler.start()
        self._running = True
        logger.info(f"SyncScheduler 已启动，间隔 {interval_hours}h")

    def stop(self) -> None:
        """停止调度器。"""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._running = False
        logger.info("SyncScheduler 已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe_progress(self, callback: Callable) -> None:
        """注册进度回调 callback(event: dict)。"""
        self._progress_callbacks.append(callback)

    def _notify(self, event: dict) -> None:
        for cb in self._progress_callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _sync_all(self) -> dict:
        """同步所有已启用的订阅。返回汇总信息。"""
        subs = self.db.list_subscriptions(enabled_only=True)

        if not subs:
            logger.info("无已启用的订阅，跳过同步")
            return {"synced": 0, "new_posts": 0}

        logger.info(f"开始同步 {len(subs)} 个订阅")

        self._notify({
            "type": "sync_started",
            "subscription_count": len(subs),
            "timestamp": datetime.now().isoformat(),
        })

        total_new = 0
        results = []

        for sub in subs:
            try:
                new_count = self._sync_one(sub)
                total_new += new_count
                results.append({
                    "sec_user_id": sub["sec_user_id"],
                    "nickname": sub["nickname"],
                    "new_posts": new_count,
                })
            except Exception as e:
                logger.error(f"同步失败 {sub['nickname']}: {e}")
                results.append({
                    "sec_user_id": sub["sec_user_id"],
                    "nickname": sub["nickname"],
                    "error": str(e),
                })

        summary = {
            "synced": len(subs),
            "new_posts": total_new,
            "results": results,
            "timestamp": datetime.now().isoformat(),
        }

        self._notify({
            "type": "sync_done",
            **summary,
        })

        logger.info(f"同步完成: {total_new} 个新帖子")
        return summary

    def _sync_one(self, sub: dict) -> int:
        """同步单个订阅，返回新帖子数量。"""
        from . import parse_aweme, parse_user_info
        from .downloader import Downloader

        nickname = sub.get("nickname", sub["sec_user_id"])
        sec_user_id = sub["sec_user_id"]
        last_aweme_id = sub.get("last_aweme_id")

        self._notify({
            "type": "sync_user_start",
            "sec_user_id": sec_user_id,
            "nickname": nickname,
        })

        session = self._session_factory()

        try:
            # 获取最新一页
            raw_posts, user_data = session.get_user_posts(sec_user_id, max_pages=1)

            if not raw_posts:
                logger.info(f"   {nickname}: 无帖子（可能需中国 IP）")
                return 0

            # 解析用户信息并更新 DB
            username = nickname
            if user_data:
                user = parse_user_info(user_data)
                username = user.unique_id or user.nickname
                self.db.upsert_user(user, sec_user_id)

            # 找到新帖（最新帖子在列表前面）
            new_posts = []
            for raw in raw_posts:
                aweme_id = str(raw.get("aweme_id", ""))
                if last_aweme_id and aweme_id == last_aweme_id:
                    break  # 到达已同步的最新帖，停止
                new_posts.append(raw)

            if last_aweme_id is None:
                # 首次同步：仅记录最新 aweme_id，不下载历史
                if raw_posts:
                    latest_id = str(raw_posts[0].get("aweme_id", ""))
                    self.db.update_subscription_sync(sec_user_id, latest_id)
                    logger.info(f"   {nickname}: 首次同步，记录 {latest_id}")
                return 0

            if not new_posts:
                logger.info(f"   {nickname}: 无新帖")
                return 0

            # 下载新帖（从旧到新，因为 reversed(new_posts) 让最旧的先下载）
            downloader = Downloader(
                base_dir=self.download_dir,
                subfolder=username,
                db=self.db,
                max_workers=self.max_workers,
                min_delay=0.5,
                max_delay=1.5,
            )

            downloaded_count = 0
            for raw in reversed(new_posts):
                aweme = parse_aweme(raw)
                if not aweme.media_items:
                    continue

                # 注册帖子到数据库
                existing = self.db.get_post_by_aweme_id(str(aweme.aweme_id))
                if not existing:
                    self.db.insert_post({
                        "aweme_id": str(aweme.aweme_id),
                        "user_id": None,  # 后续关联
                        "aweme_type": aweme.aweme_type,
                        "desc": aweme.desc,
                        "create_time": aweme.create_time,
                        "duration": getattr(aweme, "duration", 0),
                        "cover_url": getattr(aweme, "cover_url", ""),
                        "raw_json": "",
                    })

                files = downloader.download_aweme(aweme)
                if files:
                    downloaded_count += 1

            downloader.close()

            # 更新 last_aweme_id
            if raw_posts:
                latest_id = str(raw_posts[0].get("aweme_id", ""))
                self.db.update_subscription_sync(sec_user_id, latest_id)

            logger.info(f"   {nickname}: {downloaded_count} 个新帖")
            return downloaded_count

        finally:
            session.close()

    def sync_now(self) -> dict:
        """手动触发一次同步（同步模式，供 CLI/API 使用）。"""
        return self._sync_all()
