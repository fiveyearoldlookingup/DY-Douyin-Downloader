# DY — 抖音下载工具 🎵

> 抖音用户公开内容自动爬取工具。纯 Python + requests，无需浏览器，支持 **CLI / WebUI / 桌面应用** 三种使用方式。

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
</p>

## ✨ 特性

| 功能 | 说明 |
|------|------|
| 🔍 **用户爬取** | 输入用户主页链接，自动下载全部公开作品（视频 + 图集） |
| 📹 **单帖下载** | 粘贴帖子链接，下载单个视频/图集 |
| 📡 **直播录制** | 输入直播间链接，自动录制 + FFmpeg 转封装 |
| 🔄 **增量同步** | 订阅创作者，定时检查新帖自动下载（类 RSS） |
| ⚡ **多线程下载** | ThreadPoolExecutor 并行，图集账号提速 3-5x |
| 💾 **断点续爬** | 崩溃重启后仅重试中断的帖子，不重复下载 |
| 🔎 **全文搜索** | 按标题/作者搜索已下载内容 |
| 📊 **Dashboard** | 今日/本周/本月统计 + 每日图表 + 热门创作者 |
| 🛡️ **反爬池** | UA / 代理 / Cookie 轮换 + 自动故障切换 |
| 🖥️ **原生桌面** | macOS WKWebView / Windows Edge WebView2 桌面窗口 |

## 📦 安装

```bash
git clone https://github.com/fiveyearoldlookingup/DY-Douyin-Downloader.git
cd DY-Douyin-Downloader
pip install -r requirements.txt
```

## 🚀 使用

### 桌面应用（推荐）

[**下载最新版**](https://github.com/fiveyearoldlookingup/DY-Douyin-Downloader/releases)

| 平台 | 说明 |
|------|------|
| 🍎 macOS | 下载 `.dmg`，双击安装。首次打开若提示安全警告，前往「系统偏好设置 → 安全性与隐私」允许 |
| 🪟 Windows | 下载 `.zip`，解压运行 `DY抖音下载.exe`。Win10/11 自带 WebView2 运行时 |

### WebUI

```bash
python webui.py
# 浏览器打开 http://localhost:5050
```

### CLI

```bash
# 下载单个帖子
python test.py 7524544482937048320

# 爬取用户全部作品
python test.py MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU

# 从 URL
python test.py "https://www.douyin.com/user/xxx?modal_id=7524544482937048320"

# 直播录制
python test.py --live "https://live.douyin.com/723565127698"

# 订阅管理
python test.py --subscribe MS4wLjABAAAA...   # 订阅创作者
python test.py --list-subs                   # 查看订阅
python test.py --sync                        # 手动同步
```

## ⚙️ 配置

首次使用需配置 cookie（从浏览器 F12 → 网络 → 请求头复制）：

```bash
# 方式一：WebUI → 设置页面粘贴
# 方式二：编辑 config.json
{
  "cookie": "sessionid=xxx; ttwid=yyy; ...",
  "download_dir": "./downloads",
  "delay_min": 1.0,
  "delay_max": 3.0
}
```

> 💡 部分 API（如用户帖子列表）需要中国大陆 IP 或有效 cookie，详见 [CLAUDE.md](CLAUDE.md) 的 API 可用性说明。

## 🏗️ 架构

```
encrypt.py ←── session.py ←── webui.py / test.py  (入口)
  (签名)          (API会话)        (CLI / WebUI)

anti_crawl.py ──┘ (UA/代理/Cookie 池)

database.py ←── downloader.py ←── webui.py / test.py
 (SQLite)         (并行下载+状态机)
    ↑
scheduler.py ──┘ (订阅定时同步)
task_manager.py  (多任务并发)
live.py          (直播录制，独立模块)
```

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 📄 License

MIT — 详见 [LICENSE](LICENSE)

---

⭐ 如果这个项目对你有用，欢迎 Star！
