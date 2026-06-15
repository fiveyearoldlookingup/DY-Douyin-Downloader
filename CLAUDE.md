# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

**DY** — 抖音用户公开内容自动爬取工具。纯 Python 实现：requests + aBogus 签名直接调用抖音 Web API，无需浏览器。提供 CLI（`test.py`）和 Web UI（`webui.py`）两种入口。

已从"课程作业级爬虫"升级为"可长期运行的数据采集系统"，核心特性：

- **SQLite 持久化**: 替代 metadata.json，线程安全，WAL 模式
- **断点续爬**: 崩溃后重启仅重试中断的帖子
- **多线程并行下载**: ThreadPoolExecutor，图集账号提速 3-5x
- **自动增量同步**: 订阅创作者，定时检查新帖自动下载（APScheduler）
- **FTS5 全文搜索**: 支持按标题/作者搜索已下载内容
- **反爬池**: UA/代理/Cookie 轮换 + 自动故障切换
- **任务队列**: 支持多任务并发执行
- **Dashboard**: 今日/本周/本月统计 + 每日图表 + 热门创作者

- **Git 根目录**: `/Users/bump/Documents/01DevCode`（不要假设当前目录就是 git 根）

## 项目结构

```
DY/
├── douyin/
│   ├── __init__.py       # 公开 API（所有模块的导出）
│   ├── encrypt.py        # aBogus 签名算法 (SM3+RC4+Base64) + msToken 生成
│   ├── session.py        # DouyinSession — requests 会话 + 自动签名 + 分页 + 反爬池
│   ├── user.py           # 数据类 (Aweme, UserInfo, MediaItem) + API 响应解析
│   ├── downloader.py     # 流式下载 + DB 记录 + 状态机 + ThreadPoolExecutor 并行
│   ├── live.py           # 直播流录制 + URL 自动刷新 + FFmpeg 转封装
│   ├── database.py       # SQLite 数据库管理 (WAL 模式, FTS5 搜索, 迁移系统)
│   ├── scheduler.py      # 订阅定时同步 (APScheduler)
│   ├── anti_crawl.py     # UA/代理/Cookie 轮换池
│   ├── task_manager.py   # 多任务并发管理器
│   └── utils.py          # URL 解析 (extract_sec_user_id/aweme_id/web_rid)、文件名清理
├── test.py               # CLI 入口 (argparse，自动路由 URL vs ID vs live vs subscribe)
├── webui.py              # Flask Web UI (SSE 实时进度 + 文件浏览 + 配置管理 + 订阅 + 搜索 + 任务队列)
├── config.json           # 持久化配置 (cookie, proxy, delay 等)
├── dy_data.db            # SQLite 数据库（自动创建+迁移）
├── templates/            # Web UI 的 HTML 模板
├── requirements.txt      # requests, gmssl, flask, apscheduler
└── CLAUDE.md
```

## 运行方式

```bash
# 安装依赖
pip install -r requirements.txt

# ── CLI ──
# 下载单个帖子（纯数字 ID 或完整 URL）
python test.py 7524544482937048320
python test.py "https://www.douyin.com/user/xxx?modal_id=7524544482937048320"

# 爬取用户全部帖子（sec_user_id 或用户主页 URL）
python test.py MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU

# 带选项
python test.py --proxy "http://127.0.0.1:7890" --pages 5 MS4wLjABAAAA...
python test.py --cookie "sessionid=xxx; ttwid=yyy" --output ./my_downloads MS4wLjABAAAA...
python test.py --workers 8 --delay-min 2.0 --delay-max 5.0 MS4wLjABAAAA...

# 直播录制
python test.py --live "https://live.douyin.com/723565127698"
python test.py --live -f mp4 -q full_hd1 -d 1800 723565127698

# ── 订阅管理（新）──
python test.py --subscribe MS4wLjABAAAA...   # 订阅创作者
python test.py --subscribe "https://www.douyin.com/user/xxx"  # 从 URL 订阅
python test.py --list-subs                   # 列出订阅
python test.py --sync                        # 手动同步
python test.py --unsubscribe MS4wLjABAAAA...

# ── Web UI ──
python webui.py       # 启动在 http://localhost:5050

# ── 桌面应用 ──
python launcher.py                  # 原生窗口（macOS: WKWebView, Windows: Edge WebView2）
python launcher.py --server-only    # 仅启动 Flask（不弹窗口）
```

## 打包分发

```bash
# macOS → dist/DY抖音下载.app
./build_mac.sh

# Windows → dist/DY抖音下载.exe
build_win.bat
```

打包依赖 `pywebview>=5.0` + `pyinstaller>=6.0`，首次打包需安装。

## 配置（config.json）

CLI 和 WebUI 共享 `config.json`，命令行参数优先级高于配置文件：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "cookie": "sessionid=xxx; ttwid=yyy",
  "download_dir": "./downloads",
  "delay_min": 1.0,
  "delay_max": 3.0,
  "pages_per_request": 18,
  "live_quality": "hd1",
  "live_format": "flv",
  "live_output_dir": "./downloads/live"
}
```

cookie 支持两种格式：标准 `name1=value1; name2=value2` 和 JSON 字符串 `{"name1":"value1"}`。用于过反爬，需定期从浏览器更新。

> 如需启用反爬池，可添加 `"cookies": [...]` 和 `"proxies": [...]` 数组字段。单值字段仍向后兼容。

## 核心架构

### 数据库 (database.py)

`DatabaseManager` — SQLite 核心，WAL 模式 + `threading.local()` 每线程连接：

- **users** — 用户信息（nickname, sec_user_id, follower_count...）
- **posts** — 帖子 + 状态机（pending/downloading/success/failed）+ raw_json
- **files** — 文件记录（file_path, file_size, file_type）
- **subscriptions** — 订阅（sec_user_id, last_aweme_id, last_sync_at）
- **posts_fts / users_fts** — FTS5 全文搜索虚拟表（自动触发器同步）

首次运行自动从 `downloads/*/metadata.json` 迁移旧数据（幂等）。

### 下载器 (downloader.py)

`Downloader` — 并行媒体文件下载器：

- 启动时清理 zombie `downloading` → `failed`（断点续爬）
- 防双重下载：`acquire_download_slot / release_download_slot`
- ThreadPoolExecutor（默认 5 workers）并行下载
- 跨线程速率控制（共享 `_rate_lock`）

### 会话 (session.py)

`DouyinSession` — API 会话管理：

- 每个请求自动生成 aBogus 签名 + msToken
- 反爬池轮换：UARotator → ProxyPool → CookiePool
- 请求间隔随机延迟（模拟人类）
- `get_user_posts(sec_user_id)` → 逐页调用 `aweme/v1/web/aweme/post/`
- `get_post_detail(aweme_id)` → `aweme/v1/web/aweme/detail/`

### 调度器 (scheduler.py)

`SyncScheduler` — APScheduler 后台定时同步：

- 默认每 6 小时检查所有已启用订阅
- 首次订阅不下载历史（仅记录 last_aweme_id）
- 后续同步：获取最新页 → 对比 last_aweme_id → 下载新帖

### 任务管理器 (task_manager.py)

`TaskManager` — ThreadPoolExecutor（2 workers）多任务并发：
- `submit(type, target, fn)` → Task(id, status, progress, cancel_event)
- `cancel(task_id)` / `list_tasks(status_filter)` / `subscribe_events(callback)`

### WebUI (webui.py)

主要端点：

| 方法 | 路由 | 功能 |
|------|------|------|
| POST | `/api/crawl` | 启动爬取 |
| POST | `/api/cancel` | 取消当前爬取 |
| GET | `/api/events` | SSE 实时推送 |
| POST | `/api/live/start` | 开始录制 |
| POST | `/api/live/stop` | 停止录制 |
| GET | `/api/live/status` | 查询录制状态 |
| GET | `/api/files` | 文件浏览（DB 优先，DB 空时回退文件系统）|
| GET | `/api/stats` | 统计仪表盘（今日/本周/本月/每日图表/热门创作者）|
| GET | `/api/search?q=...&type=post|user` | 全文搜索（LIKE 查询，兼容中英文）|
| GET/POST | `/api/config` | 配置读写 |
| GET/DELETE | `/api/preview/<path>` | 文件预览/删除 |
| GET/POST/DELETE | `/api/subscriptions` | 订阅管理 |
| POST | `/api/subscriptions/sync` | 手动同步 |
| GET/POST/DELETE | `/api/tasks` | 任务队列 |

SSE 事件类型：`log`, `user`, `post`, `download`, `skip`, `done`, `error`, `ping`, `live_status`, `live_info`, `live_progress`, `live_stopped`, `live_error`

### 模块依赖流

```
encrypt.py  ←── session.py  ←── webui.py / test.py
                 (ABogus)         (CLI / Web UI)
anti_crawl.py ──┘ (UA/代理/Cookie 池)
                 
database.py ←── downloader.py  ←── webui.py / test.py
 (SQLite)       (状态机+并行)
   ↑
scheduler.py ──┘ (订阅同步，写入 DB)
task_manager.py (多任务调度)
live.py (独立模块，复用 session)
```

### 搜索实现说明

数据库已创建 FTS5 虚拟表（`posts_fts` / `users_fts`），但 FTS5 默认 `unicode61` tokenizer 对无空格的中文分词效果差（整个 CJK 序列被视为单个 token）。当前 `search_posts()` / `search_users()` 使用 LIKE 查询，对中英文均友好。FTS5 表保留供后续集成 jieba 分词器或自定义 tokenizer 时使用。

## 签名算法参考

- aBogus 原版: `TikTokDownloader/src/encrypt/aBogus.py`
- API 接口模板: `TikTokDownloader/src/interface/template.py`
- 基础参数 `BASE_PARAMS` 对应 `template.py` 中 `API.params`

## API 可用性

| 端点 | 境外 IP | 说明 |
|------|--------|------|
| `aweme/v1/web/aweme/detail/` | ✅ 可用 | 单个帖子详情 |
| `aweme/v1/web/aweme/post/` | ❌ 空响应 | 帖子列表，需中国 IP 或有效 cookie |
| `aweme/v1/web/user/profile/` | ❌ 空响应 | 用户资料 |
| `webcast/room/web/enter/` | ✅ 可用 | 直播房间信息 |
| 搜索类 API | ❌ 需登录 | 返回 status_code=2483 |

## 测试 URL

- 用户主页: `https://www.douyin.com/user/MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU`
- 图片集: `modal_id=7647005731895979621`
- 视频: `modal_id=7524544482937048320`

## 环境

- **平台**: macOS (Darwin), Shell: zsh
- **Python**: python3，不假设 conda
- 依赖: `requests>=2.28.0`, `gmssl>=3.2.2`, `flask>=3.0`, `apscheduler>=3.10`
