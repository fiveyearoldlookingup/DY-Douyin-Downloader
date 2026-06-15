# DY — 抖音下载工具 🎵

<p align="center">
  <img src="https://img.shields.io/github/v/release/fiveyearoldlookingup/DY-Douyin-Downloader?label=latest" alt="release">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="python">
  <img src="https://img.shields.io/github/license/fiveyearoldlookingup/DY-Douyin-Downloader" alt="license">
  <img src="https://img.shields.io/github/downloads/fiveyearoldlookingup/DY-Douyin-Downloader/total" alt="downloads">
</p>

<p align="center"><b>纯 Python 实现 · 无需浏览器 · 支持 CLI / WebUI / 桌面应用</b></p>

---

## 📑 目录

- [✨ 特性](#-特性)
- [📥 安装 & 运行](#-安装--运行)
  - [桌面应用（推荐）](#桌面应用推荐)
  - [WebUI](#webui)
  - [CLI](#cli)
- [🍪 获取 Cookie（必读）](#-获取-cookie必读)
- [⚙️ 配置参考](#️-配置参考)
- [📖 命令速查](#-命令速查)
- [🔧 常见问题](#-常见问题)
- [🏗️ 架构](#️-架构)
- [📄 License](#-license)

---

## ✨ 特性

| 功能 | 说明 |
|------|------|
| 🔍 用户爬取 | 输入用户主页链接，自动下载全部公开作品（视频 + 图集） |
| 📹 单帖下载 | 粘贴帖子链接，下载单个视频/图集 |
| 📡 直播录制 | 输入直播间链接，自动录制 + FFmpeg 转封装 |
| 🔄 增量同步 | 订阅创作者，定时检查新帖自动下载（类 RSS） |
| ⚡ 多线程下载 | ThreadPoolExecutor 并行，图集账号提速 3-5x |
| 💾 断点续爬 | 崩溃重启后仅重试中断的帖子，不重复下载 |
| 🔎 全文搜索 | 按标题/作者搜索已下载内容 |
| 📊 Dashboard | 今日/本周/本月统计 + 每日图表 + 热门创作者 |
| 🛡️ 反爬池 | UA / 代理 / Cookie 轮换 + 自动故障切换 |
| 🖥️ 原生桌面 | macOS WKWebView / Windows Edge WebView2 桌面窗口 |

---

## 📥 安装 & 运行

### 前置要求

- Python 3.10+
- （直播录制）[FFmpeg](https://ffmpeg.org/) 并加入 PATH

```bash
git clone https://github.com/fiveyearoldlookingup/DY-Douyin-Downloader.git
cd DY-Douyin-Downloader
pip install -r requirements.txt
```

### 桌面应用（推荐）

| 平台 | 下载 | 说明 |
|------|------|------|
| 🍎 macOS | [DY抖音下载_macOS.dmg](https://github.com/fiveyearoldlookingup/DY-Douyin-Downloader/releases/latest) | 双击 DMG → 拖入 Applications。首次打开若提示「无法验证开发者」，前往 **系统偏好设置 → 安全性与隐私 → 仍要打开** |
| 🪟 Windows | [DY抖音下载_Windows.zip](https://github.com/fiveyearoldlookingup/DY-Douyin-Downloader/releases/latest) | 解压 ZIP → 运行 `DY抖音下载.exe`。Win10/11 自带 WebView2；Win7 需[手动安装](https://developer.microsoft.com/microsoft-edge/webview2/) |

> 桌面应用会后台启动 Flask 服务，并弹出一个原生窗口显示 WebUI。关闭窗口即停止服务。

### WebUI

```bash
python webui.py
# 浏览器打开 http://localhost:5050
```

WebUI 提供完整的可视化操作：爬取、搜索、订阅管理、Dashboard 统计、任务队列。

### CLI

```bash
# 单帖下载
python test.py 7524544482937048320
python test.py "https://www.douyin.com/user/xxx?modal_id=7524544482937048320"

# 爬取用户全部作品
python test.py MS4wLjABAAAApedenUgRsdKSSDcrw4TE7MrMPwpPFmw_b5kIGGWQ1jU
python test.py "https://www.douyin.com/user/MS4wLjABAAAA..."
python test.py --pages 5 --workers 8 MS4wLjABAAAA...   # 翻5页，8线程

# 直播录制
python test.py --live "https://live.douyin.com/723565127698"
python test.py --live -f mp4 -q full_hd1 -d 1800 723565127698  # mp4, 超清, 30分钟

# 订阅管理
python test.py --subscribe MS4wLjABAAAA...   # 订阅
python test.py --list-subs                   # 查看
python test.py --sync                        # 手动同步
python test.py --unsubscribe MS4wLjABAAAA... # 取消
```

---

## 🍪 获取 Cookie（必读）

抖音部分 API（用户帖子列表、搜索）需要登录态 cookie，否则返回空数据。

### 步骤

1. 浏览器打开 [douyin.com](https://www.douyin.com) 并**登录**
2. 按 `F12` 打开开发者工具 → **网络 (Network)** 标签
3. 刷新页面，点击任意一个请求
4. 在 **请求头 (Request Headers)** 中找到 `Cookie:` 一行，复制完整值
5. 粘贴到 WebUI 设置页 或 `config.json` 的 `cookie` 字段

```
Cookie 示例格式: sessionid=xxx; ttwid=yyy; __ac_nonce=zzz; __ac_signature=...
```

> ⚠️ Cookie 相当于你的登录凭证，**切勿公开分享**。本项目 `.gitignore` 已排除 `config.json`。

---

## ⚙️ 配置参考

所有配置项（编辑 `config.json` 或在 WebUI 设置页修改）：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cookie` | string | `""` | 抖音登录 cookie，**必填** |
| `proxy` | string | `""` | 代理地址，如 `http://127.0.0.1:7890` |
| `download_dir` | string | `"./downloads"` | 下载保存目录 |
| `delay_min` | float | `1.0` | 请求最小间隔（秒） |
| `delay_max` | float | `3.0` | 请求最大间隔（秒） |
| `pages_per_request` | int | `18` | 每页帖子数（API 上限 18） |
| `live_quality` | string | `"hd1"` | 直播画质：`full_hd1` / `hd1` / `sd1` |
| `live_format` | string | `"flv"` | 直播格式：`flv` / `mp4` |
| `live_output_dir` | string | `"./downloads/live"` | 直播保存目录 |

### 反爬池（可选）

如需多账号/多 IP 轮换，添加数组字段：

```json
{
  "cookies": ["cookie1", "cookie2"],
  "proxies": ["http://p1:7890", "http://p2:7890"]
}
```

---

## 📖 命令速查

```bash
python test.py <帖子ID>                    # 下载单个帖子
python test.py <用户sec_user_id>           # 爬取用户全部作品
python test.py <URL>                       # 自动识别 URL 类型

python test.py --proxy <代理> <目标>       # 指定代理
python test.py --cookie <cookie> <目标>    # 指定 cookie
python test.py --output <目录> <目标>      # 指定下载目录
python test.py --workers 8 <目标>          # 并行下载线程数（默认 5）
python test.py --pages 3 <目标>            # 限制翻页数
python test.py --delay-min 2 --delay-max 5 # 请求间隔

python test.py --live <直播URL>            # 录制直播
python test.py --live -f mp4 <URL>         # mp4 格式
python test.py --live -q full_hd1 <URL>    # 超清画质
python test.py --live -d 1800 <URL>        # 录制 30 分钟

python test.py --subscribe <用户>          # 订阅创作者
python test.py --unsubscribe <用户>        # 取消订阅
python test.py --list-subs                 # 列出所有订阅
python test.py --sync                      # 手动同步所有订阅
```

---

## 🔧 常见问题

<details>
<summary><b>Q: 爬取用户时提示"未获取到任何帖子"？</b></summary>

通常是 cookie 无效或 API 地域限制。解决方案：
1. 确保 cookie 是最新的（抖音 cookie 有效期通常 1-2 天）
2. 如果使用境外 IP，部分 API 需要中国大陆 IP
3. 尝试在 config.json 中配置代理
</details>

<details>
<summary><b>Q: macOS 打开应用提示"无法验证开发者"？</b></summary>

这是 macOS Gatekeeper 机制，因为应用未经过 Apple 官方签名。解决方法：
```bash
# 终端执行
sudo xattr -rd com.apple.quarantine /path/to/DY抖音下载.app
# 或：系统偏好设置 → 安全性与隐私 → 仍要打开
```
</details>

<details>
<summary><b>Q: 数据库损坏或想重置？</b></summary>

```bash
rm dy_data.db        # 删除数据库（下次启动自动重建）
rm -rf downloads/    # 删除已下载的文件（可选）
```
</details>

<details>
<summary><b>Q: 如何让订阅自动同步？</b></summary>

WebUI 模式启动后会自动启动 APScheduler，每 6 小时检查一次订阅。如需修改间隔，编辑 `douyin/scheduler.py` 中的 `interval_hours` 参数。
</details>

<details>
<summary><b>Q: 下载速度太慢？</b></summary>

- 增加线程数：`--workers 10`
- 减小请求延迟：`--delay-min 0.5 --delay-max 1`
- 但注意太激进可能触发反爬
</details>

---

## 🏗️ 架构

```
encrypt.py ←── session.py ←── webui.py / test.py  入口
  (aBogus)       (API会话)         (CLI / WebUI)

anti_crawl.py ──┘ (UA/代理/Cookie 池)

database.py ←── downloader.py ←── webui.py / test.py
 (SQLite)         (并行+状态机)
    ↑
scheduler.py ──┘ (订阅定时同步)
task_manager.py   (多任务并发)
live.py           (直播，独立模块)
```

项目详情见 [CLAUDE.md](CLAUDE.md) 和 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 📄 License

MIT © [fiveyearoldlookingup](https://github.com/fiveyearoldlookingup)

---

⭐ **有用的话，给个 Star！**
