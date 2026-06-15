"""SQLite 数据库管理模块。

替代 metadata.json，提供线程安全、崩溃一致、可查询的持久化层。

特性:
- WAL 模式 + threading.local() 每线程连接
- Schema 版本迁移系统
- 首次运行自动从 metadata.json 导入旧数据
- 支持 Phase 2 断点续爬的状态机
- 预创建 subscriptions 表供 Phase 4 使用

用法:
    from douyin.database import DatabaseManager
    db = DatabaseManager()
    user_id = db.upsert_user(user_info, sec_user_id)
    db.insert_post({...})
"""

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Schema
# ═══════════════════════════════════════════════════════════════

SCHEMA_VERSION = 2

CREATE_TABLES_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sec_user_id     TEXT NOT NULL UNIQUE,
    nickname        TEXT NOT NULL DEFAULT '',
    unique_id       TEXT NOT NULL DEFAULT '',
    signature       TEXT NOT NULL DEFAULT '',
    avatar_url      TEXT NOT NULL DEFAULT '',
    follower_count  INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    aweme_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id      TEXT NOT NULL UNIQUE,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    aweme_type    INTEGER NOT NULL DEFAULT 0,
    desc          TEXT NOT NULL DEFAULT '',
    create_time   INTEGER NOT NULL DEFAULT 0,
    duration      INTEGER NOT NULL DEFAULT 0,
    cover_url     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','downloading','success','failed')),
    downloaded_at TEXT,
    raw_json      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_posts_user_id   ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_status    ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_aweme_id  ON posts(aweme_id);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    file_path  TEXT NOT NULL,
    file_size  INTEGER NOT NULL DEFAULT 0,
    file_type  TEXT NOT NULL DEFAULT 'video'
               CHECK(file_type IN ('video','image')),
    url        TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_post_id ON files(post_id);

-- 订阅表（Phase 4 使用，Phase 1 预创建）
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sec_user_id   TEXT NOT NULL UNIQUE,
    nickname      TEXT NOT NULL DEFAULT '',
    last_aweme_id TEXT,
    last_sync_at  TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ═══════════════════════════════════════════════════════════════
# DatabaseManager
# ═══════════════════════════════════════════════════════════════

class DatabaseManager:
    """SQLite 数据库管理器。

    线程安全：每线程持有独立连接（threading.local），
    WAL 模式允许并发读，写操作由 SQLite 内部锁序列化。

    用法:
        db = DatabaseManager("dy_data.db")
        user_id = db.upsert_user(user_info, sec_user_id)
        db.insert_post(post_dict)
        stats = db.get_stats()
    """

    def __init__(self, db_path: str | Path = "dy_data.db"):
        if isinstance(db_path, Path):
            db_path = str(db_path)
        self._db_path = db_path
        self._local = threading.local()

        # 并发下载保护（Phase 2 使用）
        self._downloading_lock = threading.Lock()
        self._downloading_set: set[str] = set()

        # 初始化表结构
        self._init_db()

    # ── 连接管理 ──────────────────────────────────────────────

    def get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（自动创建）。"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return self._local.conn

    def _execute(self, sql: str, params: tuple | dict | None = None) -> sqlite3.Cursor:
        """执行 SQL，自动获取连接。"""
        conn = self.get_connection()
        if params:
            return conn.execute(sql, params)
        return conn.execute(sql)

    def _executescript(self, sql: str) -> None:
        """执行多语句 SQL。"""
        conn = self.get_connection()
        conn.executescript(sql)

    def _fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        rows = self._execute(sql, params).fetchall()
        if rows:
            return dict(rows[0])
        return None

    def _fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        rows = self._execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _init_db(self) -> None:
        """初始化数据库：创建表 + 运行迁移。"""
        self._executescript(CREATE_TABLES_SQL)
        self._run_migrations()

    def _run_migrations(self) -> None:
        """运行 schema 版本迁移。"""
        current = self._fetchone("SELECT MAX(version) as v FROM schema_version")
        current_version = (current or {}).get("v") or 0

        if current_version < 2:
            self._migrate_v2_fts5()

        if current_version < SCHEMA_VERSION:
            self._execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self.get_connection().commit()
            logger.info(f"数据库 schema 已更新到版本 {SCHEMA_VERSION}")

    def _migrate_v2_fts5(self) -> None:
        """迁移 v2: 创建 FTS5 全文搜索虚拟表。"""
        try:
            self._executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
                    desc,
                    content='posts',
                    content_rowid='id'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS users_fts USING fts5(
                    nickname,
                    unique_id,
                    signature,
                    content='users',
                    content_rowid='id'
                );

                -- 帖子 FTS 同步触发器
                CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
                    INSERT INTO posts_fts(rowid, desc) VALUES (new.id, new.desc);
                END;

                CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
                    INSERT INTO posts_fts(posts_fts, rowid, desc) VALUES('delete', old.id, old.desc);
                END;

                CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
                    INSERT INTO posts_fts(posts_fts, rowid, desc) VALUES('delete', old.id, old.desc);
                    INSERT INTO posts_fts(rowid, desc) VALUES (new.id, new.desc);
                END;

                -- 用户 FTS 同步触发器
                CREATE TRIGGER IF NOT EXISTS users_ai AFTER INSERT ON users BEGIN
                    INSERT INTO users_fts(rowid, nickname, unique_id, signature)
                    VALUES (new.id, new.nickname, new.unique_id, new.signature);
                END;

                CREATE TRIGGER IF NOT EXISTS users_ad AFTER DELETE ON users BEGIN
                    INSERT INTO users_fts(users_fts, rowid, nickname, unique_id, signature)
                    VALUES('delete', old.id, old.nickname, old.unique_id, old.signature);
                END;

                CREATE TRIGGER IF NOT EXISTS users_au AFTER UPDATE ON users BEGIN
                    INSERT INTO users_fts(users_fts, rowid, nickname, unique_id, signature)
                    VALUES('delete', old.id, old.nickname, old.unique_id, old.signature);
                    INSERT INTO users_fts(rowid, nickname, unique_id, signature)
                    VALUES (new.id, new.nickname, new.unique_id, new.signature);
                END;
            """)
            # 回填已有数据
            self._execute("INSERT INTO posts_fts(rowid, desc) SELECT id, desc FROM posts")
            self._execute("INSERT INTO users_fts(rowid, nickname, unique_id, signature) SELECT id, nickname, unique_id, signature FROM users")
            self.get_connection().commit()
            logger.info("FTS5 全文搜索索引已创建")
        except Exception as e:
            logger.warning(f"FTS5 迁移失败（可能已存在）: {e}")

    def close(self) -> None:
        """关闭当前线程的数据库连接。"""
        if hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ── 数据迁移：从 metadata.json 导入 ─────────────────────

    def migrate_from_metadata_json(self, download_dir: str) -> int:
        """扫描 download_dir 下所有用户的 metadata.json 并导入数据库。

        幂等操作：已存在的 aweme_id 会被跳过。

        Returns:
            成功迁移的帖子数量
        """
        base = Path(download_dir)
        if not base.exists():
            return 0

        total_migrated = 0

        for user_dir in base.iterdir():
            if not user_dir.is_dir():
                continue

            meta_path = user_dir / "metadata.json"
            if not meta_path.exists():
                continue

            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if not isinstance(meta, dict) or "posts" not in meta:
                continue

            username = meta.get("username", user_dir.name)
            posts = meta.get("posts", {})

            if not posts:
                continue

            # 尝试从任意一个 post 中提取 sec_user_id（metadata.json 不存这个字段）
            # 无法获取，用 username 作为 unique_id
            sec_user_id = f"migrated:{username}"
            user_id = self.upsert_user_from_meta(username, sec_user_id)

            for aweme_id, post_data in posts.items():
                if not isinstance(post_data, dict):
                    continue

                # 检查是否已存在
                existing = self._fetchone(
                    "SELECT id FROM posts WHERE aweme_id = ?", (str(aweme_id),)
                )
                if existing:
                    continue

                post_id = self._execute(
                    """INSERT OR IGNORE INTO posts
                       (aweme_id, user_id, aweme_type, desc, create_time,
                        status, downloaded_at)
                       VALUES (?, ?, ?, ?, ?, 'success', ?)""",
                    (
                        str(aweme_id),
                        user_id,
                        post_data.get("aweme_type", 0),
                        post_data.get("desc", ""),
                        post_data.get("create_time", 0),
                        post_data.get("downloaded_at", ""),
                    ),
                ).lastrowid

                if not post_id:
                    continue

                # 迁移文件
                files = post_data.get("files", [])
                for fname in files:
                    # 推断文件路径和类型
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                        file_type = "image"
                        media_dir = user_dir / "images"
                    else:
                        file_type = "video"
                        media_dir = user_dir / "videos"

                    file_path = media_dir / fname
                    file_size = file_path.stat().st_size if file_path.exists() else 0

                    self._execute(
                        """INSERT INTO files (post_id, file_path, file_size, file_type)
                           VALUES (?, ?, ?, ?)""",
                        (post_id, str(file_path), file_size, file_type),
                    )

                total_migrated += 1

            self.get_connection().commit()

        if total_migrated:
            logger.info(f"从 metadata.json 迁移了 {total_migrated} 个帖子")

        return total_migrated

    def upsert_user_from_meta(self, username: str, sec_user_id: str) -> int:
        """为 metadata.json 迁移创建/更新用户记录。"""
        self._execute(
            """INSERT INTO users (sec_user_id, nickname, unique_id)
               VALUES (?, ?, ?)
               ON CONFLICT(sec_user_id) DO UPDATE SET
               nickname = excluded.nickname,
               updated_at = datetime('now')""",
            (sec_user_id, username, username),
        )
        self.get_connection().commit()
        row = self._fetchone("SELECT id FROM users WHERE sec_user_id = ?", (sec_user_id,))
        return row["id"] if row else 0

    # ── Users CRUD ────────────────────────────────────────────

    def upsert_user(self, user_info, sec_user_id: str) -> int:
        """创建或更新用户记录。接受 UserInfo dataclass 或 dict。

        Returns:
            user_id (int)
        """
        if hasattr(user_info, "nickname"):
            # dataclass
            nickname = user_info.nickname or ""
            unique_id = user_info.unique_id or ""
            signature = user_info.signature or ""
            avatar = user_info.avatar_url or ""
            followers = getattr(user_info, "follower_count", 0) or 0
            following = getattr(user_info, "following_count", 0) or 0
            aweme_count = getattr(user_info, "aweme_count", 0) or 0
        elif isinstance(user_info, dict):
            nickname = user_info.get("nickname", "")
            unique_id = user_info.get("unique_id", "")
            signature = user_info.get("signature", "")
            avatar = user_info.get("avatar_url", user_info.get("avatar_thumb", {}).get("url_list", [""])[0] if isinstance(user_info.get("avatar_thumb"), dict) else "")
            followers = user_info.get("follower_count", 0) or 0
            following = user_info.get("following_count", 0) or 0
            aweme_count = user_info.get("aweme_count", 0) or 0
        else:
            nickname = str(user_info)
            unique_id = ""
            signature = ""
            avatar = ""
            followers = 0
            following = 0
            aweme_count = 0

        self._execute(
            """INSERT INTO users (sec_user_id, nickname, unique_id, signature,
               avatar_url, follower_count, following_count, aweme_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sec_user_id) DO UPDATE SET
               nickname = excluded.nickname,
               unique_id = excluded.unique_id,
               signature = excluded.signature,
               avatar_url = excluded.avatar_url,
               follower_count = excluded.follower_count,
               following_count = excluded.following_count,
               aweme_count = excluded.aweme_count,
               updated_at = datetime('now')""",
            (sec_user_id, nickname, unique_id, signature, avatar,
             followers, following, aweme_count),
        )
        self.get_connection().commit()
        row = self._fetchone("SELECT id FROM users WHERE sec_user_id = ?", (sec_user_id,))
        return row["id"] if row else 0

    def get_user_by_sec_user_id(self, sec_user_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM users WHERE sec_user_id = ?", (sec_user_id,))

    def get_user_by_id(self, user_id: int) -> dict | None:
        return self._fetchone("SELECT * FROM users WHERE id = ?", (user_id,))

    def list_users(self) -> list[dict]:
        return self._fetchall(
            "SELECT u.*, COUNT(p.id) as post_count "
            "FROM users u LEFT JOIN posts p ON p.user_id = u.id "
            "GROUP BY u.id ORDER BY u.nickname"
        )

    def count_users(self) -> int:
        row = self._fetchone("SELECT COUNT(*) as c FROM users")
        return row["c"] if row else 0

    # ── Posts CRUD ────────────────────────────────────────────

    def insert_post(self, post: dict) -> int | None:
        """插入帖子记录。如果 aweme_id 已存在则忽略。

        post dict 应包含: aweme_id, user_id, aweme_type, desc,
                        create_time, duration, cover_url, raw_json

        Returns:
            post_id 或 None（已存在）
        """
        try:
            self._execute(
                """INSERT OR IGNORE INTO posts
                   (aweme_id, user_id, aweme_type, desc, create_time,
                    duration, cover_url, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post.get("aweme_id", ""),
                    post.get("user_id"),
                    post.get("aweme_type", 0),
                    post.get("desc", ""),
                    post.get("create_time", 0),
                    post.get("duration", 0),
                    post.get("cover_url", ""),
                    post.get("raw_json", ""),
                ),
            )
            self.get_connection().commit()
            row = self._fetchone(
                "SELECT id FROM posts WHERE aweme_id = ?",
                (post.get("aweme_id", ""),),
            )
            return row["id"] if row else None
        except Exception:
            return None

    def update_post_status(
        self, aweme_id: str, status: str, downloaded_at: str | None = None
    ) -> None:
        """更新帖子下载状态。"""
        if downloaded_at:
            self._execute(
                "UPDATE posts SET status = ?, downloaded_at = ? WHERE aweme_id = ?",
                (status, downloaded_at, aweme_id),
            )
        else:
            self._execute(
                "UPDATE posts SET status = ? WHERE aweme_id = ?",
                (status, aweme_id),
            )
        self.get_connection().commit()

    def get_post_by_aweme_id(self, aweme_id: str) -> dict | None:
        return self._fetchone("SELECT * FROM posts WHERE aweme_id = ?", (str(aweme_id),))

    def get_posts_by_user_id(
        self, user_id: int, status: str | None = None
    ) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM posts WHERE user_id = ? AND status = ? ORDER BY create_time DESC",
                (user_id, status),
            )
        return self._fetchall(
            "SELECT * FROM posts WHERE user_id = ? ORDER BY create_time DESC",
            (user_id,),
        )

    def count_posts_by_status(self, status: str | None = None) -> int:
        if status:
            row = self._fetchone(
                "SELECT COUNT(*) as c FROM posts WHERE status = ?", (status,)
            )
        else:
            row = self._fetchone("SELECT COUNT(*) as c FROM posts")
        return row["c"] if row else 0

    def count_posts_since(self, date_str: str) -> int:
        """统计指定日期以来的帖子数。date_str 格式: 'YYYY-MM-DD'"""
        row = self._fetchone(
            "SELECT COUNT(*) as c FROM posts WHERE downloaded_at >= ?",
            (date_str,),
        )
        return row["c"] if row else 0

    # ── Files CRUD ────────────────────────────────────────────

    def insert_file(self, file_record: dict) -> int | None:
        """插入文件记录。返回 file_id。"""
        try:
            self._execute(
                """INSERT INTO files (post_id, file_path, file_size, file_type, url)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    file_record.get("post_id"),
                    file_record.get("file_path", ""),
                    file_record.get("file_size", 0),
                    file_record.get("file_type", "video"),
                    file_record.get("url", ""),
                ),
            )
            self.get_connection().commit()
            return self._execute("SELECT last_insert_rowid()").fetchone()[0]
        except Exception:
            return None

    def get_files_by_post_id(self, post_id: int) -> list[dict]:
        return self._fetchall("SELECT * FROM files WHERE post_id = ?", (post_id,))

    def count_files(self) -> int:
        row = self._fetchone("SELECT COUNT(*) as c FROM files")
        return row["c"] if row else 0

    def count_files_since(self, date_str: str) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) as c FROM files WHERE created_at >= ?",
            (date_str,),
        )
        return row["c"] if row else 0

    def sum_file_sizes(self) -> int:
        row = self._fetchone("SELECT COALESCE(SUM(file_size), 0) as s FROM files")
        return row["s"] if row else 0

    # ── Subscriptions CRUD（Phase 4 使用）───────────────────

    def add_subscription(self, sec_user_id: str, nickname: str = "") -> int | None:
        self._execute(
            """INSERT OR IGNORE INTO subscriptions (sec_user_id, nickname)
               VALUES (?, ?)""",
            (sec_user_id, nickname),
        )
        self.get_connection().commit()
        row = self._fetchone(
            "SELECT id FROM subscriptions WHERE sec_user_id = ?", (sec_user_id,)
        )
        return row["id"] if row else None

    def remove_subscription(self, sec_user_id: str) -> None:
        self._execute("DELETE FROM subscriptions WHERE sec_user_id = ?", (sec_user_id,))
        self.get_connection().commit()

    def list_subscriptions(self, enabled_only: bool = False) -> list[dict]:
        if enabled_only:
            return self._fetchall(
                "SELECT * FROM subscriptions WHERE enabled = 1 ORDER BY nickname"
            )
        return self._fetchall("SELECT * FROM subscriptions ORDER BY nickname")

    def update_subscription_sync(
        self, sec_user_id: str, last_aweme_id: str
    ) -> None:
        self._execute(
            """UPDATE subscriptions
               SET last_aweme_id = ?, last_sync_at = datetime('now')
               WHERE sec_user_id = ?""",
            (last_aweme_id, sec_user_id),
        )
        self.get_connection().commit()

    # ── 断点续爬支持（Phase 2）────────────────────────────────

    def mark_stale_downloads(self) -> int:
        """将崩溃残留的 'downloading' 帖子标记为 'failed'。

        应在 Downloader 初始化时调用。
        Returns: 被标记的帖子数量
        """
        self._execute(
            "UPDATE posts SET status = 'failed' WHERE status = 'downloading'"
        )
        self.get_connection().commit()
        return self._execute("SELECT changes()").fetchone()[0]

    def acquire_download_slot(self, aweme_id: str) -> bool:
        """尝试获取下载槽位（防止同一帖子被多线程重复下载）。"""
        with self._downloading_lock:
            if aweme_id in self._downloading_set:
                return False
            self._downloading_set.add(aweme_id)
            return True

    def release_download_slot(self, aweme_id: str) -> None:
        """释放下载槽位。"""
        with self._downloading_lock:
            self._downloading_set.discard(aweme_id)

    def is_downloading(self, aweme_id: str) -> bool:
        """检查帖子是否正在被另一线程下载。"""
        with self._downloading_lock:
            return aweme_id in self._downloading_set

    # ── 统计查询（Phase 7 预埋）────────────────────────────────

    def get_stats(self) -> dict:
        """获取全局统计摘要。"""
        total_posts = self.count_posts_by_status()
        success = self.count_posts_by_status("success")
        failed = self.count_posts_by_status("failed")
        total_files = self.count_files()
        total_size = self.sum_file_sizes()

        # 最近一次下载
        last = self._fetchone(
            "SELECT downloaded_at FROM posts WHERE status = 'success' "
            "ORDER BY downloaded_at DESC LIMIT 1"
        )
        last_crawl = last["downloaded_at"] if last else ""

        return {
            "total_posts": total_posts,
            "success_posts": success,
            "failed_posts": failed,
            "total_files": total_files,
            "total_users": self.count_users(),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 1),
            "last_crawl": last_crawl,
        }

    def get_daily_stats(self, days: int = 14) -> list[dict]:
        """获取最近 N 天的每日下载统计。"""
        return self._fetchall(
            """SELECT DATE(downloaded_at) as day,
                      COUNT(*) as posts,
                      COALESCE(SUM(
                        (SELECT COUNT(*) FROM files WHERE files.post_id = posts.id)
                      ), 0) as files
               FROM posts
               WHERE status = 'success'
                 AND downloaded_at >= DATE('now', ?)
               GROUP BY day
               ORDER BY day DESC""",
            (f"-{days} days",),
        )

    def get_failure_rate(self) -> float:
        """获取下载失败率（百分比）。"""
        total = self.count_posts_by_status()
        if total == 0:
            return 0.0
        failed = self.count_posts_by_status("failed")
        return round(failed / total * 100, 1)

    def get_top_users(self, limit: int = 5) -> list[dict]:
        """获取下载帖子最多的用户。"""
        return self._fetchall(
            """SELECT u.nickname, u.unique_id, COUNT(p.id) as post_count,
                      COALESCE(SUM((SELECT COUNT(*) FROM files f WHERE f.post_id = p.id)), 0) as file_count
               FROM users u
               JOIN posts p ON p.user_id = u.id
               WHERE p.status = 'success'
               GROUP BY u.id
               ORDER BY post_count DESC
               LIMIT ?""",
            (limit,),
        )

    # ── 搜索（Phase 5 预埋）───────────────────────────────────

    def search_posts(
        self, query: str, limit: int = 20, offset: int = 0,
        sort: str = "relevance",
    ) -> list[dict]:
        """搜索帖子（LIKE 查询，对中英文均友好）。

        FTS5 的 unicode61 tokenizer 对无空格的中文分词效果不好，
        所以主搜索用 LIKE。大数量时仍可受益于 FTS5 索引（当查询匹配到英文/数字 token 时）。

        Args:
            query: 搜索关键词
            limit: 返回条数
            offset: 分页偏移
            sort: "relevance" | "time"
        """
        if not query.strip():
            return []

        like = f"%{query}%"
        order = "p.create_time DESC" if sort == "time" else "p.create_time DESC"

        return self._fetchall(
            f"""SELECT p.*, u.nickname, u.unique_id as author_unique_id
               FROM posts p
               LEFT JOIN users u ON u.id = p.user_id
               WHERE p.status = 'success'
                 AND (p.desc LIKE ? OR u.nickname LIKE ?)
               ORDER BY {order}
               LIMIT ? OFFSET ?""",
            (like, like, limit, offset),
        )

    def search_users(self, query: str, limit: int = 20) -> list[dict]:
        """搜索用户（LIKE 查询）。"""
        if not query.strip():
            return []

        like = f"%{query}%"
        return self._fetchall(
            """SELECT u.*, COUNT(p.id) as post_count
               FROM users u
               LEFT JOIN posts p ON p.user_id = u.id
               WHERE u.nickname LIKE ? OR u.unique_id LIKE ?
               GROUP BY u.id
               ORDER BY post_count DESC
               LIMIT ?""",
            (like, like, limit),
        )

    # ── 文件浏览查询（WebUI /api/files 使用）─────────────────

    def get_browse_data(self) -> dict:
        """获取 WebUI 文件浏览器所需的数据结构。

        Returns:
            {
                "users": {username: {unique_id, videos:[], images:[], post_count, file_count, total_size_mb}},
                "total_videos": int,
                "total_images": int,
                "total_users": int,
            }
        """
        users = {}
        total_videos = 0
        total_images = 0

        rows = self._fetchall("""
            SELECT u.unique_id as username, u.nickname,
                   f.file_path, f.file_size, f.file_type, f.created_at,
                   p.aweme_id
            FROM files f
            JOIN posts p ON p.id = f.post_id
            LEFT JOIN users u ON u.id = p.user_id
            ORDER BY f.created_at DESC
        """)

        for row in rows:
            username = row["username"] or "（未分组）"
            if username not in users:
                users[username] = {
                    "unique_id": username,
                    "nickname": row["nickname"] or username,
                    "videos": [],
                    "images": [],
                    "post_count": 0,
                    "file_count": 0,
                    "total_size_mb": 0.0,
                    "_seen_posts": set(),
                }

            user_data = users[username]
            file_path = row["file_path"]
            file_name = os.path.basename(file_path)
            file_size = row["file_size"]
            size_mb = round(file_size / (1024 * 1024), 1)
            file_type = row["file_type"]

            # 生成相对路径用于预览
            rel_path = f"{username}/{file_type}s/{file_name}"

            file_entry = {
                "name": file_name,
                "size_mb": size_mb,
                "size_bytes": file_size,
                "path": rel_path,
                "date": row["created_at"][:10] if row["created_at"] else "",
            }

            if file_type == "video":
                user_data["videos"].append(file_entry)
                total_videos += 1
            else:
                user_data["images"].append(file_entry)
                total_images += 1

            user_data["file_count"] += 1
            user_data["total_size_mb"] = round(
                user_data["total_size_mb"] + size_mb, 1
            )

            aweme_id = row["aweme_id"]
            if aweme_id not in user_data["_seen_posts"]:
                user_data["_seen_posts"].add(aweme_id)
                user_data["post_count"] += 1

        # 清理内部字段
        for u in users.values():
            del u["_seen_posts"]

        return {
            "users": users,
            "total_videos": total_videos,
            "total_images": total_images,
            "total_users": len(users),
        }
