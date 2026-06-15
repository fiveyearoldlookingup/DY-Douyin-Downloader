#!/usr/bin/env python3
"""DY WebUI — 抖音爬取工具 Web 界面。

启动: python webui.py
访问: http://localhost:5050
"""

import json
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from douyin import (
    DatabaseManager,
    DouyinSession,
    Downloader,
    LiveRecorder,
    LiveStreamOffline,
    LiveStreamNotFound,
    SyncScheduler,
    TaskManager,
    TaskType,
    TaskStatus,
    parse_aweme,
    parse_user_info,
    extract_sec_user_id,
    extract_aweme_id,
    extract_web_rid,
)

# ── 路径 ──
BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
CONFIG_FILE = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "dy_data.db"

# ── Flask 应用 ──
app = Flask(__name__)

# ── 全局数据库 ──
db = DatabaseManager(str(DB_PATH))

# ── 后台爬取任务 ──
_task_queue: queue.Queue | None = None
_task_thread: threading.Thread | None = None
_task_cancel = threading.Event()

# ── 后台直播录制任务 ──
_live_queue: queue.Queue | None = None
_live_thread: threading.Thread | None = None
_live_recorder: LiveRecorder | None = None


# ═══════════════════════════════════════════════════════════════
# 共享工具函数（消除 webui.py ↔ test.py 重复）
# ═══════════════════════════════════════════════════════════════

def load_config() -> dict:
    """加载 config.json，不存在则返回空字典。"""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_download_dir(config: dict | None = None) -> str:
    """从配置获取下载目录。"""
    cfg = config or load_config()
    return cfg.get("download_dir", "./downloads")


def is_post_url(s: str) -> bool:
    """判断是否包含帖子 modal_id。"""
    return "modal_id=" in s


def is_user_url(s: str) -> bool:
    """判断输入是否是用户主页链接（非帖子链接）。"""
    return "/user/" in s and "modal_id" not in s


def is_live_url(s: str) -> bool:
    """判断输入是否是直播链接或 web_rid。"""
    return (
        "live.douyin.com" in s
        or ("/" not in s and s.strip().isdigit() and len(s.strip()) < 20)
    )


# ═══════════════════════════════════════════════════════════════
# SSE 推送
# ═══════════════════════════════════════════════════════════════

def _push_event(event: dict):
    """向 SSE 队列推送爬取事件。"""
    if _task_queue:
        _task_queue.put(event)


def _push_live_event(event: dict):
    """向 SSE 队列推送直播事件。"""
    if _live_queue:
        _live_queue.put(event)


# ═══════════════════════════════════════════════════════════════
# 后台任务
# ═══════════════════════════════════════════════════════════════

def _get_username_from_detail(detail: dict) -> str:
    """从帖子详情中提取作者唯一标识。"""
    author = detail.get("author", {})
    return author.get("unique_id") or author.get("nickname") or ""


def _make_session() -> DouyinSession:
    """创建带配置的 DouyinSession。"""
    cfg = load_config()
    cookie = cfg.get("cookie", "")
    proxy = cfg.get("proxy") or None
    return DouyinSession(cookie=cookie, proxy=proxy)


def _run_crawl(target: str = "", max_pages: int | None = None, *, task=None, crawl_target: str = "", **kwargs):
    """后台爬取任务（运行在独立线程中）。"""
    global _task_cancel

    # 支持两种调用方式：直接调用（target positional）和 TaskManager 调用（crawl_target kwarg）
    target = target or crawl_target

    session = _make_session()

    try:
        # ── 判断目标类型 ──
        if is_post_url(target):
            aweme_id = extract_aweme_id(target)
            _push_event({"type": "log", "msg": f"🔍 获取帖子: {aweme_id}"})
            detail = session.get_post_detail(aweme_id)
            if detail is None:
                _push_event({"type": "error", "msg": "获取帖子详情失败"})
                return
            aweme = parse_aweme(detail)
            username = _get_username_from_detail(detail)

            # 注册用户到数据库
            author = detail.get("author", {})
            if author and db:
                user_data = {
                    "nickname": author.get("nickname", ""),
                    "unique_id": author.get("unique_id", ""),
                    "signature": author.get("signature", ""),
                    "avatar_url": author.get("avatar_thumb", {}).get("url_list", [""])[0]
                    if isinstance(author.get("avatar_thumb"), dict) else "",
                    "follower_count": author.get("follower_count", 0),
                    "following_count": author.get("following_count", 0),
                    "aweme_count": author.get("aweme_count", 0),
                }
                sec_user_id = author.get("sec_uid", "")
                if sec_user_id:
                    user_db_id = db.upsert_user(user_data, sec_user_id)

            post_downloader = Downloader(
                base_dir=str(DOWNLOAD_DIR),
                subfolder=username,
                db=db,
            )
            if post_downloader.is_aweme_downloaded(aweme.aweme_id):
                _push_event({"type": "skip", "aweme_id": aweme.aweme_id, "desc": aweme.desc, "username": username})
                return
            _push_event({"type": "post", "desc": aweme.desc, "media_count": len(aweme.media_items), "username": username})
            downloaded = post_downloader.download_aweme(aweme)
            post_downloader.close()
            _push_event({"type": "done", "total_posts": 1, "total_files": len(downloaded), "username": username})

        elif is_user_url(target):
            sec_user_id = extract_sec_user_id(target)
            _crawl_user(session, sec_user_id, max_pages)

        elif target.isdigit() and len(target) >= 15:
            _push_event({"type": "log", "msg": f"🔍 获取帖子: {target}"})
            detail = session.get_post_detail(target)
            if detail is None:
                _push_event({"type": "error", "msg": "获取帖子详情失败"})
                return
            aweme = parse_aweme(detail)
            username = _get_username_from_detail(detail)

            # 注册用户到数据库
            author = detail.get("author", {})
            if author and db:
                user_data = {
                    "nickname": author.get("nickname", ""),
                    "unique_id": author.get("unique_id", ""),
                    "signature": author.get("signature", ""),
                    "avatar_url": author.get("avatar_thumb", {}).get("url_list", [""])[0]
                    if isinstance(author.get("avatar_thumb"), dict) else "",
                    "follower_count": author.get("follower_count", 0),
                    "following_count": author.get("following_count", 0),
                    "aweme_count": author.get("aweme_count", 0),
                }
                sec_user_id = author.get("sec_uid", "")
                if sec_user_id:
                    db.upsert_user(user_data, sec_user_id)

            post_downloader = Downloader(
                base_dir=str(DOWNLOAD_DIR),
                subfolder=username,
                db=db,
            )
            if post_downloader.is_aweme_downloaded(aweme.aweme_id):
                _push_event({"type": "skip", "aweme_id": aweme.aweme_id, "desc": aweme.desc, "username": username})
                post_downloader.close()
                return
            _push_event({"type": "post", "desc": aweme.desc, "media_count": len(aweme.media_items), "username": username})
            downloaded = post_downloader.download_aweme(aweme)
            post_downloader.close()
            _push_event({"type": "done", "total_posts": 1, "total_files": len(downloaded), "username": username})

        else:
            _crawl_user(session, target, max_pages)

    except Exception as e:
        _push_event({"type": "error", "msg": str(e)})
    finally:
        session.close()


def _crawl_user(session: DouyinSession, sec_user_id: str, max_pages: int | None):
    """爬取用户全部帖子。"""
    global _task_cancel

    _push_event({"type": "log", "msg": f"📱 爬取用户: {sec_user_id}"})
    if max_pages:
        _push_event({"type": "log", "msg": f"   限制: {max_pages} 页"})

    raw_posts, user_data = session.get_user_posts(sec_user_id, max_pages=max_pages)

    # 获取用户名并注册用户到数据库
    username = ""
    user_db_id = None
    if user_data:
        user = parse_user_info(user_data)
        username = user.unique_id or user.nickname
        _push_event({"type": "user", "nickname": user.nickname, "unique_id": user.unique_id,
                      "follower_count": user.follower_count, "aweme_count": user.aweme_count})

        if db:
            user_db_id = db.upsert_user(user, sec_user_id)

    # 创建下载器
    user_downloader = Downloader(
        base_dir=str(DOWNLOAD_DIR),
        subfolder=username,
        db=db,
    )

    if not raw_posts:
        _push_event({"type": "error", "msg": "未获取到帖子 — cookie 可能过期，请更新"})
        user_downloader.close()
        return

    total_downloaded = 0
    post_count = 0
    skipped_count = 0

    for raw in raw_posts:
        if _task_cancel.is_set():
            _push_event({"type": "log", "msg": "⏹ 用户取消"})
            user_downloader.close()
            return

        aweme = parse_aweme(raw)
        if not aweme.media_items:
            continue

        # 关联帖子到用户
        if user_db_id and db:
            existing = db.get_post_by_aweme_id(str(aweme.aweme_id))
            if not existing:
                db.insert_post({
                    "aweme_id": str(aweme.aweme_id),
                    "user_id": user_db_id,
                    "aweme_type": aweme.aweme_type,
                    "desc": aweme.desc,
                    "create_time": aweme.create_time,
                    "duration": getattr(aweme, 'duration', 0),
                    "cover_url": getattr(aweme, 'cover_url', ''),
                    "raw_json": json.dumps(raw, ensure_ascii=False),
                })
            elif not existing.get("user_id"):
                user_downloader.set_user_id_for_aweme(str(aweme.aweme_id), user_db_id)

        # 检查是否已下载
        if user_downloader.is_aweme_downloaded(aweme.aweme_id):
            skipped_count += 1
            _push_event({"type": "skip", "aweme_id": aweme.aweme_id,
                          "desc": aweme.desc[:50]})
            continue

        post_count += 1
        label = "🎬" if aweme.is_video else "🖼"
        _push_event({"type": "post", "desc": aweme.desc, "media_count": len(aweme.media_items),
                      "label": label, "index": post_count})

        downloaded = user_downloader.download_aweme(aweme)
        total_downloaded += len(downloaded)

    if skipped_count > 0:
        _push_event({"type": "log", "msg": f"⏭ 跳过 {skipped_count} 个已下载的帖子"})

    user_downloader.close()

    _push_event({"type": "done", "total_posts": post_count, "total_files": total_downloaded,
                  "skipped": skipped_count, "username": username})


def _run_live_recording(
    web_rid: str,
    output_dir: str,
    duration: float | None,
    quality: str,
    output_format: str,
    session: DouyinSession,
):
    """后台直播录制任务。"""
    global _live_recorder

    try:
        _live_recorder = LiveRecorder(session, output_dir=output_dir, quality=quality, output_format=output_format)

        # 解析直播间
        _push_live_event({"type": "live_status", "status": "connecting", "msg": "正在连接直播间..."})
        info = _live_recorder.resolve(web_rid)

        _push_live_event({
            "type": "live_info",
            "title": info.title,
            "nickname": info.nickname,
            "web_rid": info.web_rid,
            "quality": quality,
            "online_users": info.online_users,
            "output_path": _live_recorder.output_path,
        })

        # 进度报告线程
        def _report_progress():
            last_report = 0.0
            while _live_recorder and _live_recorder.is_recording:
                elapsed = _live_recorder.elapsed
                if elapsed - last_report >= 2.0:
                    bytes_written = _live_recorder.bytes_written
                    rate = bytes_written / elapsed if elapsed > 0 else 0
                    _push_live_event({
                        "type": "live_progress",
                        "elapsed": elapsed,
                        "bytes": bytes_written,
                        "size_mb": round(bytes_written / (1024 * 1024), 1),
                        "rate_mbps": round(rate / (1024 * 1024), 2),
                    })
                    last_report = elapsed
                time.sleep(1)

        progress_thread = threading.Thread(target=_report_progress, daemon=True)
        progress_thread.start()

        # 开始录制
        filepath = _live_recorder.record(
            duration=duration,
            on_progress=lambda b, e: None,
        )

        elapsed = _live_recorder.elapsed
        size_mb = _live_recorder.bytes_written / (1024 * 1024)
        _push_live_event({
            "type": "live_stopped",
            "filepath": filepath,
            "duration": elapsed,
            "size_mb": round(size_mb, 1),
            "msg": f"录制完成: {elapsed:.0f}s / {size_mb:.1f}MB",
        })

    except LiveStreamNotFound as e:
        _push_live_event({"type": "live_error", "msg": f"直播间不存在: {e}"})
    except LiveStreamOffline as e:
        _push_live_event({"type": "live_error", "msg": f"未开播: {e}"})
    except Exception as e:
        _push_live_event({"type": "live_error", "msg": str(e)})
    finally:
        session.close()
        _live_recorder = None


# ═══════════════════════════════════════════════════════════════
# Flask 路由
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    global _task_queue, _task_thread, _task_cancel

    data = request.get_json()
    target = data.get("target", "").strip()
    max_pages = data.get("max_pages") or None

    if not target:
        return jsonify({"error": "请输入 URL 或 ID"}), 400

    # 取消旧任务
    if _task_thread and _task_thread.is_alive():
        _task_cancel.set()
        _task_thread.join(timeout=5)

    _task_cancel = threading.Event()
    _task_queue = queue.Queue()
    _task_thread = threading.Thread(target=_run_crawl, args=(target, max_pages), daemon=True)
    _task_thread.start()

    return jsonify({"status": "started"})


@app.route("/api/events")
def api_events():
    """SSE 端点 — 实时推送爬取和直播录制进度。"""
    global _task_queue, _live_queue

    def generate():
        while True:
            event = None
            # 优先 crawl 队列，其次 live 队列
            for q in (_task_queue, _live_queue):
                if q:
                    try:
                        event = q.get(timeout=0.5)
                        break
                    except queue.Empty:
                        continue

            if event:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    global _task_cancel
    _task_cancel.set()
    return jsonify({"status": "cancelled"})


# ── 直播录制 API ──

@app.route("/api/live/start", methods=["POST"])
def api_live_start():
    """开始录制直播流。"""
    global _live_queue, _live_thread, _live_recorder

    data = request.get_json()
    target = data.get("target", "").strip()
    duration = data.get("duration") or None
    quality = data.get("quality", "hd1")
    output_format = data.get("format", "flv")

    if not target:
        return jsonify({"error": "请输入直播 URL 或 web_rid"}), 400

    try:
        web_rid = extract_web_rid(target)
    except Exception:
        return jsonify({"error": "无法解析直播 URL"}), 400

    # 取消旧录制任务
    if _live_recorder and _live_recorder.is_recording:
        _live_recorder.stop()
    if _live_thread and _live_thread.is_alive():
        _live_thread.join(timeout=5)

    cfg = load_config()
    output_dir = data.get("output_dir") or cfg.get("live_output_dir", "./downloads/live")
    session = _make_session()

    _live_queue = queue.Queue()
    _live_thread = threading.Thread(
        target=_run_live_recording,
        args=(web_rid, output_dir, duration, quality, output_format, session),
        daemon=True,
    )
    _live_thread.start()

    return jsonify({"status": "started", "web_rid": web_rid})


@app.route("/api/live/stop", methods=["POST"])
def api_live_stop():
    """停止当前直播录制。"""
    global _live_recorder

    if _live_recorder and _live_recorder.is_recording:
        _live_recorder.stop()
        return jsonify({"status": "stopping"})
    return jsonify({"status": "idle"})


@app.route("/api/live/status", methods=["GET"])
def api_live_status():
    """获取当前直播录制状态。"""
    global _live_recorder

    if _live_recorder and _live_recorder.is_recording:
        return jsonify({
            "status": "recording",
            "elapsed": _live_recorder.elapsed,
            "bytes": _live_recorder.bytes_written,
            "size_mb": round(_live_recorder.bytes_written / (1024 * 1024), 1),
            "output_path": _live_recorder.output_path,
        })
    elif _live_recorder:
        return jsonify({"status": "idle"})
    else:
        return jsonify({"status": "idle"})


# ── 订阅管理 API ──

# 全局同步调度器（main() 中启动）
_sync_scheduler: SyncScheduler | None = None


@app.route("/api/subscriptions", methods=["GET"])
def api_list_subscriptions():
    """列出所有订阅。"""
    subs = db.list_subscriptions()
    return jsonify({"subscriptions": subs})


@app.route("/api/subscriptions", methods=["POST"])
def api_add_subscription():
    """添加订阅。接受 sec_user_id 或用户主页 URL。"""
    data = request.get_json()
    target = data.get("target", "").strip()

    if not target:
        return jsonify({"error": "请输入 sec_user_id 或用户主页 URL"}), 400

    # 提取 sec_user_id
    if is_user_url(target):
        sec_user_id = extract_sec_user_id(target)
    elif is_post_url(target):
        return jsonify({"error": "请输入用户主页链接，不是帖子链接"}), 400
    else:
        sec_user_id = target.strip()

    # 获取用户信息
    session = _make_session()
    try:
        _, user_data = session.get_user_posts(sec_user_id, max_pages=1)
        if user_data:
            user = parse_user_info(user_data)
            nickname = user.nickname or sec_user_id
            db.upsert_user(user, sec_user_id)
        else:
            nickname = sec_user_id
    except Exception:
        nickname = sec_user_id
    finally:
        session.close()

    sub_id = db.add_subscription(sec_user_id, nickname)
    if sub_id is None:
        return jsonify({"error": "订阅已存在"}), 409

    # 后台记录最新帖（首次同步：不下载历史）
    def _init_sync():
        s = _make_session()
        try:
            raw_posts, _ = s.get_user_posts(sec_user_id, max_pages=1)
            if raw_posts:
                latest_id = str(raw_posts[0].get("aweme_id", ""))
                db.update_subscription_sync(sec_user_id, latest_id)
        except Exception:
            pass
        finally:
            s.close()

    threading.Thread(target=_init_sync, daemon=True).start()

    return jsonify({"status": "subscribed", "sec_user_id": sec_user_id, "nickname": nickname})


@app.route("/api/subscriptions/<sec_user_id>", methods=["DELETE"])
def api_remove_subscription(sec_user_id):
    """删除订阅。"""
    db.remove_subscription(sec_user_id)
    return jsonify({"status": "removed"})


@app.route("/api/subscriptions/sync", methods=["POST"])
def api_trigger_sync():
    """手动触发一次同步。"""
    if _sync_scheduler:
        summary = _sync_scheduler.sync_now()
        return jsonify({"status": "sync_done", **summary})
    else:
        # 调度器未启动，直接执行一次同步
        from douyin.scheduler import SyncScheduler
        scheduler = SyncScheduler(
            db=db,
            download_dir=str(DOWNLOAD_DIR),
            session_factory=_make_session,
        )
        summary = scheduler.sync_now()
        return jsonify({"status": "sync_done", **summary})


# ── 搜索 API ──

# ── Phase 8: 任务队列 API ──

_task_manager = TaskManager(max_workers=2)


@app.route("/api/tasks", methods=["POST"])
def api_submit_task():
    """提交新任务。"""
    data = request.get_json()
    target = data.get("target", "").strip()
    task_type_str = data.get("type", "crawl")
    max_pages = data.get("max_pages")

    if not target:
        return jsonify({"error": "需要 target"}), 400

    if task_type_str == "crawl":
        task = _task_manager.submit(
            TaskType.CRAWL, target,
            _run_crawl,
            crawl_target=target, max_pages=max_pages,
        )
    elif task_type_str == "live":
        duration = data.get("duration") or None
        quality = data.get("quality", "hd1")
        output_format = data.get("format", "flv")
        task = _task_manager.submit(
            TaskType.LIVE, target,
            _run_live_task_wrapper,
            live_target=target, duration=duration,
            quality=quality, output_format=output_format,
        )
    else:
        return jsonify({"error": f"未知任务类型: {task_type_str}"}), 400

    return jsonify(task.to_dict())


def _run_live_task_wrapper(task, **kwargs):
    """直播任务的 TaskManager 兼容包装器。"""
    global _live_recorder
    target = kwargs.get("live_target", "")
    web_rid = extract_web_rid(target)
    duration = kwargs.get("duration")
    quality = kwargs.get("quality", "hd1")
    output_format = kwargs.get("format", "flv")

    cfg = load_config()
    output_dir = cfg.get("live_output_dir", "./downloads/live")
    session = _make_session()

    _run_live_recording(web_rid, output_dir, duration, quality, output_format, session)


@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    """列出所有任务。"""
    status_filter = request.args.get("status")
    status = TaskStatus(status_filter) if status_filter else None
    tasks = _task_manager.list_tasks(status)
    return jsonify({"tasks": tasks, "running": _task_manager.running_count})


@app.route("/api/tasks/<task_id>", methods=["GET"])
def api_get_task(task_id):
    """获取单个任务。"""
    task = _task_manager.get_task(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_cancel_task(task_id):
    """取消任务。"""
    ok = _task_manager.cancel(task_id)
    if not ok:
        return jsonify({"error": "任务无法取消（可能已完成）"}), 409
    return jsonify({"cancelled": True})


# ── 搜索 API ──

@app.route("/api/search")
def api_search():
    """全文搜索帖子和用户。"""
    q = request.args.get("q", "").strip()
    search_type = request.args.get("type", "post")
    sort = request.args.get("sort", "relevance")
    limit = min(int(request.args.get("limit", 20)), 100)
    page = max(int(request.args.get("page", 1)), 1)
    offset = (page - 1) * limit

    if not q or len(q) < 1:
        return jsonify({"results": [], "total": 0, "query": q})

    if search_type == "user":
        results = db.search_users(q, limit=limit)
    else:
        results = db.search_posts(q, limit=limit, offset=offset, sort=sort)

    return jsonify({
        "results": results,
        "query": q,
        "type": search_type,
        "page": page,
        "limit": limit,
    })


# ── 文件浏览 API（优先 DB，回退文件系统）──

@app.route("/api/files")
def api_files():
    """列出已下载文件，按用户 → 类型两级分组。

    优先从 SQLite 数据库查询，DB 为空时回退到文件系统扫描。
    """
    # 尝试从 DB 获取
    if db and db.count_files() > 0:
        data = db.get_browse_data()
        return jsonify(data)

    # 回退到文件系统扫描
    return jsonify(_scan_filesystem())


def _scan_filesystem() -> dict:
    """文件系统扫描（向后兼容回退）。"""
    users = {}
    total_videos = 0
    total_images = 0

    if not DOWNLOAD_DIR.exists():
        return {"users": {}, "total_videos": 0, "total_images": 0, "total_users": 0}

    for user_entry in sorted(DOWNLOAD_DIR.iterdir()):
        if not user_entry.is_dir():
            continue

        has_videos = (user_entry / "videos").is_dir()
        has_images = (user_entry / "images").is_dir()

        if not has_videos and not has_images:
            continue

        username = user_entry.name
        user_data = {
            "unique_id": username,
            "videos": [],
            "images": [],
            "post_count": 0,
            "file_count": 0,
            "total_size_mb": 0.0,
        }

        # 读取 metadata.json
        meta_path = user_entry / "metadata.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    user_data["post_count"] = meta.get("total_posts", 0)
                    user_data["unique_id"] = meta.get("username", username)
            except (json.JSONDecodeError, OSError):
                pass

        for media_type in ("images", "videos"):
            media_dir = user_entry / media_type
            if not media_dir.is_dir():
                continue

            files = []
            for f in sorted(media_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if not f.is_file() or f.name == "metadata.json":
                    continue
                size_mb = f.stat().st_size / (1024 * 1024)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                rel_path = str(Path(username) / media_type / f.name)
                files.append({
                    "name": f.name,
                    "size_mb": round(size_mb, 1),
                    "size_bytes": f.stat().st_size,
                    "path": rel_path,
                    "date": mtime,
                })

            user_data[media_type] = files
            user_data["file_count"] += len(files)
            user_data["total_size_mb"] = round(
                user_data["total_size_mb"] + sum(f["size_mb"] for f in files), 1
            )

            if media_type == "videos":
                total_videos += len(files)
            else:
                total_images += len(files)

        if user_data["videos"] or user_data["images"]:
            users[username] = user_data

    # 兼容旧结构
    root_videos = DOWNLOAD_DIR / "videos"
    root_images = DOWNLOAD_DIR / "images"
    if root_videos.is_dir() or root_images.is_dir():
        default_files = {"videos": [], "images": [], "post_count": 0, "file_count": 0,
                         "total_size_mb": 0.0, "unique_id": "（未分组）"}
        for media_type, media_dir in (("videos", root_videos), ("images", root_images)):
            if not media_dir.is_dir():
                continue
            for f in sorted(media_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if not f.is_file():
                    continue
                size_mb = f.stat().st_size / (1024 * 1024)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
                rel_path = str(Path(media_type) / f.name)
                default_files[media_type].append({
                    "name": f.name, "size_mb": round(size_mb, 1),
                    "path": rel_path, "date": mtime,
                })
                default_files["file_count"] += 1
                default_files["total_size_mb"] = round(default_files["total_size_mb"] + size_mb, 1)
                if media_type == "videos":
                    total_videos += 1
                else:
                    total_images += 1
        if default_files["videos"] or default_files["images"]:
            users["（未分组）"] = default_files

    return {
        "users": users,
        "total_videos": total_videos,
        "total_images": total_images,
        "total_users": len(users),
    }


# ── 统计 API（优先 DB，回退文件系统）──

@app.route("/api/stats")
def api_stats():
    """全局下载统计。优先使用 DB 聚合查询，DB 为空时回退文件扫描。"""
    if db and db.count_files() > 0:
        stats = db.get_stats()
        # 补充 time-range 数据
        import datetime as _dt
        today = _dt.date.today().strftime("%Y-%m-%d")
        week_ago = (_dt.date.today() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
        stats["today"] = {
            "posts": db.count_posts_since(today),
            "files": db.count_files_since(today),
        }
        stats["this_week"] = {
            "posts": db.count_posts_since(week_ago),
            "files": db.count_files_since(week_ago),
        }
        stats["failure_rate"] = db.get_failure_rate()
        stats["top_users"] = db.get_top_users(5)
        stats["daily_stats"] = db.get_daily_stats(14)
        return jsonify(stats)

    # 回退文件系统
    return jsonify(_scan_stats_filesystem())


def _scan_stats_filesystem() -> dict:
    """文件系统统计（向后兼容回退）。"""
    total_users = 0
    total_videos = 0
    total_images = 0
    total_size_bytes = 0
    last_crawl = ""

    def _count_dir(media_dir, media_type):
        nonlocal total_size_bytes, total_videos, total_images
        if not media_dir.is_dir():
            return
        for f in media_dir.iterdir():
            if f.is_file() and f.name != "metadata.json":
                total_size_bytes += f.stat().st_size
                if media_type == "videos":
                    total_videos += 1
                else:
                    total_images += 1

    if DOWNLOAD_DIR.exists():
        for user_entry in DOWNLOAD_DIR.iterdir():
            if not user_entry.is_dir():
                continue
            has_media = (user_entry / "videos").is_dir() or (user_entry / "images").is_dir()
            if not has_media:
                continue
            total_users += 1

            meta_path = user_entry / "metadata.json"
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    crawled = meta.get("crawled_at", "")
                    if crawled and crawled > last_crawl:
                        last_crawl = crawled
                except (json.JSONDecodeError, OSError):
                    pass

            _count_dir(user_entry / "videos", "videos")
            _count_dir(user_entry / "images", "images")

        root_videos = DOWNLOAD_DIR / "videos"
        root_images = DOWNLOAD_DIR / "images"
        if (root_videos.is_dir() or root_images.is_dir()):
            if total_users == 0:
                total_users = 1
            _count_dir(root_videos, "videos")
            _count_dir(root_images, "images")

    return {
        "total_users": total_users,
        "total_videos": total_videos,
        "total_images": total_images,
        "total_files": total_videos + total_images,
        "total_size_mb": round(total_size_bytes / (1024 * 1024), 1),
        "last_crawl": last_crawl,
        "today": {"posts": 0, "files": 0},
        "this_week": {"posts": 0, "files": 0},
        "failure_rate": 0.0,
        "top_users": [],
        "daily_stats": [],
    }


# ── 配置管理 ──

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    cookie = cfg.get("cookie", "")
    preview = cookie[:30] + "..." if len(cookie) > 30 else cookie
    return jsonify({
        "cookie": cookie,
        "cookie_preview": preview,
        "proxy": cfg.get("proxy", ""),
        "download_dir": cfg.get("download_dir", "./downloads"),
        "delay_min": cfg.get("delay_min", 1.0),
        "delay_max": cfg.get("delay_max", 3.0),
        "pages_per_request": cfg.get("pages_per_request", 18),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json()
    cfg = load_config()
    for key in ("cookie", "proxy", "download_dir", "delay_min", "delay_max", "pages_per_request"):
        if key in data:
            cfg[key] = data[key]
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return jsonify({"status": "saved"})


# ── 文件预览 / 删除 ──

@app.route("/api/preview/<path:filepath>", methods=["GET", "DELETE"])
def api_preview(filepath):
    """提供已下载文件的静态访问和删除。"""
    full_path = DOWNLOAD_DIR / filepath
    full_path = full_path.resolve()
    if not str(full_path).startswith(str(DOWNLOAD_DIR.resolve())):
        return "Forbidden", 403
    if not full_path.exists():
        return "Not found", 404

    if request.method == "DELETE":
        try:
            full_path.unlink()
            # 清理空目录
            parent = full_path.parent
            if parent != DOWNLOAD_DIR.resolve() and not any(parent.iterdir()):
                parent.rmdir()
                grandparent = parent.parent
                if grandparent != DOWNLOAD_DIR.resolve() and not any(grandparent.iterdir()):
                    grandparent.rmdir()
            return jsonify({"status": "deleted"})
        except OSError as e:
            return jsonify({"error": str(e)}), 500

    return send_file(full_path)


# ── 入口 ──

def main():
    global _sync_scheduler

    # Phase 1: 尝试从旧 metadata.json 迁移数据
    download_dir = get_download_dir()
    if db.count_files() == 0:
        migrated = db.migrate_from_metadata_json(download_dir)
        if migrated:
            print(f"📦 从 metadata.json 迁移了 {migrated} 个帖子到 SQLite")

    # Phase 4: 启动后台同步调度器
    _sync_scheduler = SyncScheduler(
        db=db,
        download_dir=str(DOWNLOAD_DIR),
        session_factory=_make_session,
    )
    _sync_scheduler.start(interval_hours=6)

    print("=" * 50)
    print("🖥  DY WebUI 启动中...")
    print(f"   DB: {DB_PATH}")
    print(f"   订阅同步: 每 6 小时")
    print("   打开浏览器: http://localhost:5050")
    print("=" * 50)

    try:
        app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
    finally:
        if _sync_scheduler:
            _sync_scheduler.stop()


if __name__ == "__main__":
    main()
