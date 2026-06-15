# DY 架构文档

## 设计决策

### 为什么用纯 requests 而不是 Playwright？

最初设计考虑过 Playwright 浏览器自动化来绕过签名，但最终选择了纯 HTTP 方案：

- **签名可离线计算**: aBogus 算法是纯数学运算（SM3 + RC4 + Base64），不依赖浏览器环境
- **性能优势**: requests 比浏览器快 10-100 倍，内存占用可忽略
- **可部署性**: 无需安装 Chromium，可在无头服务器上运行
- **msToken 可随机生成**: 经测试，随机 156 位字符串即可正常工作

### 签名流程

```
用户请求参数 (params dict)
    │
    ▼
urlencode(params, safe="=")          # URL 编码，保留 = 号
    │
    ▼
ABogus.get_value(params_str, "GET")  # 生成 a_bogus 签名
    │   ├── SM3 国密哈希（对 UA 和参数做摘要）
    │   ├── RC4 流加密
    │   └── 自定义 Base64 编码（替换 +/= 字符）
    │
    ▼
{api_url}?{params_str}&a_bogus={signature}
```

### 请求参数结构

所有 API 请求都携带 `BASE_PARAMS`（模拟 Chrome 浏览器环境），包含设备指纹信息：
- `device_platform=webapp`, `aid=6383`
- 屏幕分辨率、CPU 核心数、内存大小
- 浏览器/引擎/OS 版本号
- 网络类型（`effective_type=4g`）

这些参数对签名结果有影响，不能随意修改。

### 数据模型层次

```
API JSON 响应 (dict)
    │
    ├── parse_user_info() → UserInfo (dataclass)
    │     nickname, unique_id, signature, avatar_url,
    │     follower_count, following_count, aweme_count
    │
    └── parse_aweme() → Aweme (dataclass)
          aweme_id, aweme_type, desc, create_time
          │
          └── media_items: list[MediaItem]
                url, file_extension (.mp4 / .jpg / .webp / .png)
```

### 下载去重机制

`Downloader` 在用户目录下维护 `metadata.json`：

```json
{
  "username": "some_user",
  "posts": {
    "7524544482937048320": {
      "aweme_id": "7524544482937048320",
      "desc": "帖子描述",
      "create_time": 1734567890,
      "aweme_type": 0,
      "files": ["20250614_帖子描述_2937048320.mp4"],
      "downloaded_at": "2026-06-14T12:00:00"
    }
  },
  "total_posts": 1,
  "total_files": 1
}
```

- 下载前按 `aweme_id` 查 `posts` 字典，存在则跳过
- `--no-skip` 可强制重新下载

### WebUI 事件流架构

```
浏览器 (SSE EventSource)
    │  GET /api/events (text/event-stream)
    ▼
Flask SSE 端点 ←── queue.Queue ── 后台爬取线程
                                      │
                   POST /api/crawl ───┘
                   POST /api/cancel → threading.Event.set()
```

- **单任务模型**: 新爬取任务自动取消旧任务（`_task_cancel.set()` + `join(timeout=5)`）
- **事件类型**: `log`, `user`, `post`, `download`, `skip`, `done`, `error`, `ping`（心跳）
- 下载文件通过 `/api/preview/<path>` 直接 serve，DELETE 时自动清理空目录

### 目录结构

```
downloads/
├── {username1}/
│   ├── metadata.json
│   ├── videos/
│   │   └── 20250614_帖子描述_2937048320.mp4
│   └── images/
│       └── 20250614_图片帖_0057318962_1.jpg
├── {username2}/
│   └── ...
└── videos/    ← 旧版兼容：无用户分组时的遗留目录
└── images/
```

API `/api/files` 和 `/api/stats` 兼容两种结构。

## API 端点详情

### post/ — 用户帖子列表
```
GET https://www.douyin.com/aweme/v1/web/aweme/post/
  ?sec_user_id=MS4wLjABAAAA...
  &max_cursor=0           # 分页游标，0=第一页
  &count=18               # 每页数量（最大~20）
  &msToken=...            # 随机生成
  &a_bogus=...            # 自动签名
  &[BASE_PARAMS...]
```

响应关键字段:
- `aweme_list[]` — 帖子数组
- `max_cursor` — 下一页游标
- `has_more` — 是否还有更多页
- `user` — 用户信息（仅第一页包含）

### detail/ — 单个帖子详情
```
GET https://www.douyin.com/aweme/v1/web/aweme/detail/
  ?aweme_id=7524544482937048320
  &msToken=...
  &a_bogus=...
  &[BASE_PARAMS...]
```

响应: `{ "aweme_detail": { ... } }`

## 已知限制

1. **境外 IP**: `aweme/post/` 对非中国 IP 返回空响应体（200 OK 但 body 为空）
2. **搜索接口**: 需要登录态，未实现
3. **cookie 时效**: 从浏览器提取的 cookie 约 24-48 小时过期
4. **直播**: `aweme_type=4` 目前跳过，未实现直播流录制
5. **视频清晰度**: 当前取 `play_addr_h264` (无水印但可能非最高清)，`download_addr` 有水印但更高清
