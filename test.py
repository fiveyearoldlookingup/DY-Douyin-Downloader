#!/usr/bin/env python3
"""DY — 抖音公开内容爬取工具。

用法:
    # 下载单个帖子（支持完整 URL 或纯 ID）
    python test.py "https://www.douyin.com/user/xxx?modal_id=7524544482937048320"
    python test.py 7524544482937048320

    # 爬取用户全部帖子（支持完整 URL 或纯 sec_user_id）
    python test.py "https://www.douyin.com/user/MS4wLjABAAAA..."
    python test.py MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU

    # 带代理（中国 IP 可解锁用户爬取）
    python test.py --proxy "http://127.0.0.1:7890" "https://www.douyin.com/user/xxx"

    # 限制页数 / 自定义下载目录
    python test.py --pages 5 -o ./my_downloads "https://www.douyin.com/user/xxx"
"""

import argparse
import json
import os
import sys

from douyin import (
    DatabaseManager,
    DouyinSession,
    Downloader,
    LiveRecorder,
    LiveStreamOffline,
    LiveStreamNotFound,
    SyncScheduler,
    parse_aweme,
    parse_user_info,
    extract_sec_user_id,
    extract_aweme_id,
    extract_web_rid,
)

# ── 配置文件 ──
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "dy_data.db")


def load_config() -> dict:
    """加载 config.json，文件不存在则返回空字典。"""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_user_url(s: str) -> bool:
    """判断输入是用户主页链接还是 sec_user_id。"""
    return "/user/" in s and "modal_id" not in s


def is_post_url(s: str) -> bool:
    """判断输入是否包含帖子 modal_id。"""
    return "modal_id=" in s


def is_live_url(s: str) -> bool:
    """判断输入是否是直播链接或 web_rid。"""
    return "live.douyin.com" in s or ("/" not in s and s.strip().isdigit() and len(s.strip()) < 20)


def download_post(
    aweme_id: str,
    downloader: Downloader,
    session: DouyinSession,
):
    """下载单个帖子。"""
    print(f"🔍 获取帖子: {aweme_id}")
    detail = session.get_post_detail(aweme_id)
    if detail is None:
        print("❌ 获取帖子详情失败")
        return

    aweme = parse_aweme(detail)
    print(f"   {aweme.desc[:60]}")
    print(f"   类型: {aweme.aweme_type}  媒体数: {len(aweme.media_items)}")

    if not aweme.media_items:
        print("⚠ 无可下载媒体（可能是直播或其他类型）")
        return

    downloader.download_aweme(aweme)


def crawl_user(
    sec_user_id: str,
    downloader: Downloader,
    session: DouyinSession,
    db: DatabaseManager | None = None,
    max_pages: int | None = None,
):
    """爬取用户全部帖子。"""
    print(f"📱 爬取用户: {sec_user_id}")
    if max_pages:
        print(f"   限制页数: {max_pages}")

    raw_posts, user_data = session.get_user_posts(sec_user_id, max_pages=max_pages)

    user_db_id = None
    if user_data:
        user = parse_user_info(user_data)
        print(f"\n👤 {user.nickname} (@{user.unique_id})")
        print(f"   粉丝: {user.follower_count}  作品: {user.aweme_count}")
        if user.signature:
            print(f"   简介: {user.signature}")

        # 注册用户到数据库
        if db:
            user_db_id = db.upsert_user(user, sec_user_id)

    if not raw_posts:
        print("\n⚠ 未获取到帖子（境外 IP 需配置 --proxy 中国代理）")
        return

    count = 0
    for raw in raw_posts:
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
                downloader.set_user_id_for_aweme(str(aweme.aweme_id), user_db_id)

        count += 1
        label = "🎬" if aweme.is_video else "🖼"
        print(f"\n[{count}] {label} {aweme.desc[:60]}")
        downloader.download_aweme(aweme)

    print(f"\n✅ 完成！下载了 {count} 个帖子")


def record_live(
    target: str,
    session: DouyinSession,
    output_dir: str,
    duration: float | None = None,
    quality: str = "hd1",
    output_format: str = "flv",
):
    """录制直播流。"""
    web_rid = extract_web_rid(target)
    recorder = LiveRecorder(session, output_dir=output_dir, quality=quality, output_format=output_format)

    try:
        info = recorder.resolve(web_rid)
    except LiveStreamNotFound as e:
        print(f"❌ {e}")
        return
    except LiveStreamOffline as e:
        print(f"⚠ {e}")
        return

    print(f"\n📺 {info.title}")
    print(f"   👤 {info.nickname}")
    print(f"   🎬 画质: {quality}  →  FLV 地址已获取")
    if info.online_users:
        print(f"   👥 在线: {info.online_users}")
    print(f"   📁 输出: {recorder.output_path}")

    if duration:
        print(f"   ⏱ 时长限制: {duration:.0f}s")
    print()

    try:
        recorder.record(duration=duration)
    except KeyboardInterrupt:
        recorder.stop()
        print("⏹ 已停止")


def main():
    parser = argparse.ArgumentParser(
        description="DY — 抖音公开内容爬取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test.py 7524544482937048320                           # 下载单个帖子
  python test.py "https://...?modal_id=7524544482937048320"   # 从链接下载
  python test.py MS4wLjABAAAA...                               # 爬取用户全部帖子
  python test.py --proxy http://127.0.0.1:7890 MS4wLjABAAAA... # 使用代理
  python test.py --pages 3 -o ./downloads MS4wLjABAAAA...     # 限制3页+指定目录
        """,
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="帖子 ID、用户 sec_user_id、或完整 URL",
    )
    parser.add_argument(
        "--proxy", "-p",
        default=None,
        help="代理地址，如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--cookie", "-k",
        default=None,
        help="浏览器 cookie 字符串（name1=value1; name2=value2），用于过反爬",
    )
    parser.add_argument(
        "--pages", "-n",
        type=int,
        default=None,
        help="最大爬取页数（不指定 = 爬完）",
    )
    parser.add_argument(
        "--output", "-o",
        default="./downloads",
        help="下载目录（默认 ./downloads）",
    )
    parser.add_argument(
        "--count", "-c",
        type=int,
        default=18,
        help="每页帖子数（默认 18）",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=1.0,
        help="请求最小间隔秒数（默认 1.0）",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=3.0,
        help="请求最大间隔秒数（默认 3.0）",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="不跳过已存在的文件（默认会跳过）",
    )
    parser.add_argument(
        "--live", "-l",
        action="store_true",
        help="启用直播录制模式（输入为 live.douyin.com 链接或 web_rid）",
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=None,
        help="直播录制时长（秒），0=手动停止",
    )
    parser.add_argument(
        "--quality", "-q",
        default="hd1",
        choices=["full_hd1", "hd1", "sd1", "sd2"],
        help="直播画质（默认 hd1）",
    )
    parser.add_argument(
        "--format", "-f",
        default="flv",
        choices=["flv", "mp4"],
        help="输出格式: flv 或 mp4（默认 flv，mp4 需要 ffmpeg）",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=5,
        help="并行下载线程数（默认 5）",
    )
    parser.add_argument(
        "--subscribe", "-s",
        type=str,
        default=None,
        metavar="TARGET",
        help="订阅创作者（支持 sec_user_id 或用户主页 URL）",
    )
    parser.add_argument(
        "--unsubscribe",
        type=str,
        default=None,
        metavar="TARGET",
        help="取消订阅创作者",
    )
    parser.add_argument(
        "--list-subs",
        action="store_true",
        help="列出所有订阅",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="手动触发一次订阅同步",
    )

    args = parser.parse_args()

    # ── 加载配置文件，命令行参数优先 ──
    cfg = load_config()
    proxy = args.proxy or cfg.get("proxy") or None
    cookie = args.cookie or cfg.get("cookie") or ""
    output_dir = args.output or cfg.get("download_dir") or "./downloads"
    delay_min = args.delay_min if args.delay_min != 1.0 else cfg.get("delay_min", 1.0)
    delay_max = args.delay_max if args.delay_max != 3.0 else cfg.get("delay_max", 3.0)
    pages_per_req = args.count if args.count != 18 else cfg.get("pages_per_request", 18)

    # ── 初始化数据库 ──
    db = DatabaseManager(DB_PATH)

    # 首次运行自动迁移旧 metadata.json
    if db.count_files() == 0:
        migrated = db.migrate_from_metadata_json(output_dir)
        if migrated:
            print(f"📦 从 metadata.json 迁移了 {migrated} 个帖子到 SQLite")

    # ── 初始化 session（订阅命令也需要）──
    session = DouyinSession(
        proxy=proxy,
        cookie=cookie,
        min_delay=delay_min,
        max_delay=delay_max,
    )

    # ── 订阅命令（无需 target）──
    if args.list_subs:
        subs = db.list_subscriptions()
        if not subs:
            print("📭 暂无订阅")
        else:
            print(f"📋 订阅列表 ({len(subs)}):")
            for s in subs:
                status = "✅" if s["enabled"] else "⏸"
                last = s.get("last_sync_at", "从未")
                print(f"   {status} {s['nickname']} ({s['sec_user_id'][:20]}...) 上次同步: {last}")
        session.close()
        db.close()
        return

    if args.sync:
        print("🔄 开始同步订阅...")
        scheduler = SyncScheduler(
            db=db,
            download_dir=output_dir,
            session_factory=lambda: DouyinSession(
                proxy=proxy, cookie=cookie,
                min_delay=delay_min, max_delay=delay_max,
            ),
        )
        summary = scheduler.sync_now()
        print(f"✅ 同步完成: {summary['synced']} 个订阅, {summary['new_posts']} 个新帖")
        for r in summary.get("results", []):
            if "error" in r:
                print(f"   ❌ {r['nickname']}: {r['error']}")
            else:
                print(f"   📥 {r['nickname']}: {r['new_posts']} 个新帖")
        session.close()
        db.close()
        return

    if args.subscribe:
        sub_target = args.subscribe.strip()
        sec_user_id = sub_target
        if is_user_url(sub_target):
            sec_user_id = extract_sec_user_id(sub_target)
        elif is_post_url(sub_target):
            print("❌ 请输入用户主页链接，不是帖子链接")
            session.close()
            db.close()
            return

        print(f"🔍 查找用户: {sec_user_id}")
        _, user_data = session.get_user_posts(sec_user_id, max_pages=1)
        nickname = sec_user_id
        if user_data:
            user = parse_user_info(user_data)
            nickname = user.nickname or sec_user_id
            db.upsert_user(user, sec_user_id)

        db.add_subscription(sec_user_id, nickname)
        print(f"✅ 已订阅: {nickname}")

        print("📡 记录最新帖...")
        raw_posts, _ = session.get_user_posts(sec_user_id, max_pages=1)
        if raw_posts:
            latest_id = str(raw_posts[0].get("aweme_id", ""))
            db.update_subscription_sync(sec_user_id, latest_id)
            print(f"   记录最新帖: {latest_id}")
        session.close()
        db.close()
        return

    if args.unsubscribe:
        sec_user_id = args.unsubscribe.strip()
        if is_user_url(sec_user_id):
            sec_user_id = extract_sec_user_id(sec_user_id)
        db.remove_subscription(sec_user_id)
        print(f"✅ 已取消订阅: {sec_user_id}")
        session.close()
        db.close()
        return

    # ── 无 target：交互提示 ──
    if not args.target:
        print("用法: python test.py <URL或ID> [选项]")
        print()
        print("示例:")
        print('  python test.py "https://www.douyin.com/user/xxx?modal_id=7524544482937048320"')
        print('  python test.py MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU')
        print('  python test.py --proxy "http://127.0.0.1:7890" "https://www.douyin.com/user/xxx"')
        print()
        print("更多: python test.py --help")
        db.close()
        return

    target = args.target.strip() if args.target else ""

    # ── 初始化 ──
    session = DouyinSession(
        proxy=proxy,
        cookie=cookie,
        min_delay=delay_min,
        max_delay=delay_max,
    )
    downloader = Downloader(
        base_dir=output_dir,
        skip_existing=not args.no_skip,
        db=db,
        max_workers=args.workers,
        min_delay=delay_min,
        max_delay=delay_max,
    )

    # ── 路由：根据输入类型分发 ──
    try:
        if args.subscribe:
            # 订阅创作者
            sub_target = args.subscribe.strip()
            sec_user_id = sub_target
            if is_user_url(sub_target):
                sec_user_id = extract_sec_user_id(sub_target)
            elif is_post_url(sub_target):
                print("❌ 请输入用户主页链接，不是帖子链接")
                return

            # 获取用户信息
            print(f"🔍 查找用户: {sec_user_id}")
            _, user_data = session.get_user_posts(sec_user_id, max_pages=1)
            nickname = sec_user_id
            if user_data:
                user = parse_user_info(user_data)
                nickname = user.nickname or sec_user_id
                db.upsert_user(user, sec_user_id)

            db.add_subscription(sec_user_id, nickname)
            print(f"✅ 已订阅: {nickname}")

            # 首次同步：记录最新帖
            print("📡 记录最新帖...")
            raw_posts, _ = session.get_user_posts(sec_user_id, max_pages=1)
            if raw_posts:
                latest_id = str(raw_posts[0].get("aweme_id", ""))
                db.update_subscription_sync(sec_user_id, latest_id)
                print(f"   记录最新帖: {latest_id}")

        elif args.unsubscribe:
            sec_user_id = args.unsubscribe.strip()
            if is_user_url(sec_user_id):
                sec_user_id = extract_sec_user_id(sec_user_id)
            db.remove_subscription(sec_user_id)
            print(f"✅ 已取消订阅: {sec_user_id}")

        elif args.list_subs:
            subs = db.list_subscriptions()
            if not subs:
                print("📭 暂无订阅")
            else:
                print(f"📋 订阅列表 ({len(subs)}):")
                for s in subs:
                    status = "✅" if s["enabled"] else "⏸"
                    last = s.get("last_sync_at", "从未")
                    print(f"   {status} {s['nickname']} ({s['sec_user_id'][:20]}...) 上次同步: {last}")

        elif args.sync:
            print("🔄 开始同步订阅...")
            scheduler = SyncScheduler(
                db=db,
                download_dir=output_dir,
                session_factory=lambda: DouyinSession(
                    proxy=proxy,
                    cookie=cookie,
                    min_delay=delay_min,
                    max_delay=delay_max,
                ),
            )
            summary = scheduler.sync_now()
            print(f"✅ 同步完成: {summary['synced']} 个订阅, {summary['new_posts']} 个新帖")
            for r in summary.get("results", []):
                if "error" in r:
                    print(f"   ❌ {r['nickname']}: {r['error']}")
                else:
                    print(f"   📥 {r['nickname']}: {r['new_posts']} 个新帖")

        elif args.live:
            # 直播录制模式
            live_output_dir = args.output if args.output != "./downloads" else cfg.get("live_output_dir", "./downloads/live")
            quality = args.quality or cfg.get("live_quality", "hd1")
            output_format = args.format or cfg.get("live_format", "flv")
            record_live(target, session, live_output_dir, duration=args.duration, quality=quality, output_format=output_format)

        elif is_post_url(target):
            # 完整帖子 URL → 提取 aweme_id 下载
            aweme_id = extract_aweme_id(target)
            download_post(aweme_id, downloader, session)

        elif is_user_url(target):
            # 完整用户 URL → 提取 sec_user_id 爬取
            sec_user_id = extract_sec_user_id(target)
            crawl_user(sec_user_id, downloader, session, db=db, max_pages=args.pages)

        else:
            # 纯 ID — 靠长度推断类型
            if target.isdigit() and len(target) >= 15:
                download_post(target, downloader, session)
            else:
                crawl_user(target, downloader, session, db=db, max_pages=args.pages)

    finally:
        downloader.close()
        session.close()
        db.close()


if __name__ == "__main__":
    main()
