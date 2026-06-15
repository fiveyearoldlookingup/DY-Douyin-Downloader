"""抖音直播流录制模块。

通过 live.douyin.com API 解析直播间信息，获取 FLV 拉流地址，
支持持续录制、断线重连和 URL 自动刷新。

用法:
    from douyin.live import LiveRecorder
    from douyin.session import DouyinSession

    session = DouyinSession()
    recorder = LiveRecorder(session)
    info = recorder.resolve("123456789")
    recorder.record(duration=60)
"""

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

from .encrypt import USERAGENT, generate_fake_ms_token
from .utils import sanitize_filename, ensure_dir

logger = logging.getLogger(__name__)

# ── API 端点 ──
API_ROOM_ENTER = "https://live.douyin.com/webcast/room/web/enter/"

# ── 画质优先级（高到低）──
QUALITY_RANK = ["FULL_HD1", "HD1", "SD1", "SD2"]

# FLV URL 刷新间隔（秒），需小于 ~180s 的有效期
URL_REFRESH_INTERVAL = 150

# 录制块大小
CHUNK_SIZE = 65536  # 64KB


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════

class LiveError(Exception):
    """直播相关基础异常。"""


class LiveStreamNotFound(LiveError):
    """直播间不存在或 web_rid 无效。"""


class LiveStreamOffline(LiveError):
    """直播间存在但当前未开播（status != 2）。"""


class LiveRecordingError(LiveError):
    """录制过程中发生的错误（网络、磁盘等）。"""


# ═══════════════════════════════════════════════════════════════
# 数据类
# ═══════════════════════════════════════════════════════════════

@dataclass
class LiveStreamInfo:
    """直播间解析后的元数据。"""

    room_id: str = ""               # 长 room_id
    web_rid: str = ""               # URL 中的短 ID
    status: int = 0                 # 2=直播中, 4=已结束
    title: str = ""                 # 直播标题
    nickname: str = ""              # 主播昵称
    cover_url: str = ""             # 封面图
    stream_url: str = ""            # 最优画质 FLV 拉流地址
    stream_urls: dict[str, str] = field(default_factory=dict)  # 全部 FLV 地址
    online_users: int = 0           # 在线观众数

    @property
    def is_live(self) -> bool:
        return self.status == 2


# ═══════════════════════════════════════════════════════════════
# LiveRecorder
# ═══════════════════════════════════════════════════════════════

class LiveRecorder:
    """抖音直播流录制器。

    特性:
    - 自动解析直播间信息（web_rid → room_id → FLV URL）
    - FLV URL 自动刷新（每 ~150 秒，应对 ~180s 有效期）
    - 断线自动重连续写
    - 支持时长限制和手动停止
    - 进度回调（供 WebUI 使用）

    用法:
        session = DouyinSession(cookie="...")
        recorder = LiveRecorder(session, output_dir="./downloads/live")
        info = recorder.resolve("123456789")
        print(f"开播: {info.title} — {info.nickname}")
        path = recorder.record(duration=3600)
        print(f"录制完成: {path}")
    """

    def __init__(
        self,
        session: "DouyinSession",  # type: ignore[name-defined]
        output_dir: str = "./downloads/live",
        quality: str = "hd1",
        output_format: str = "flv",
    ):
        """
        Args:
            session: DouyinSession 实例（复用 cookie/proxy）
            output_dir: 录制文件输出目录
            quality: 首选画质: full_hd1 / hd1 / sd1 / sd2
            output_format: 输出格式: flv 或 mp4（需要 ffmpeg）
        """
        self._session = session
        self.output_dir = output_dir
        self.quality = quality.lower()
        self.output_format = output_format.lower()

        # 持久标识
        self._web_rid: str = ""
        self._output_path: str = ""
        self._final_output_path: str = ""

        # 运行时状态
        self._stop_event = threading.Event()
        self._current_url: str = ""
        self._bytes_written: int = 0
        self._start_time: float = 0.0
        self._last_url_refresh: float = 0.0
        self._consecutive_failures: int = 0

        ensure_dir(self.output_dir)

    # ── 公开 API ──────────────────────────────────────────────

    def resolve(self, web_rid: str) -> LiveStreamInfo:
        """解析直播间信息，获取流地址。

        调用 live.douyin.com/webcast/room/web/enter/ 接口，
        提取房间元数据和 FLV 拉流地址。

        Args:
            web_rid: 直播间短 ID（live.douyin.com/{web_rid} 中的部分）

        Returns:
            LiveStreamInfo 包含房间信息和最优流地址

        Raises:
            LiveStreamNotFound: 直播间不存在
            LiveStreamOffline: 未开播
        """
        self._web_rid = web_rid

        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "device_platform": "web",
            "web_rid": web_rid,
            "msToken": generate_fake_ms_token(),
        }

        url = f"{API_ROOM_ENTER}?{urlencode(params)}"
        logger.info(f"解析直播间: {web_rid}")

        try:
            resp = self._session._session.get(
                url,
                headers={
                    "User-Agent": USERAGENT,
                    "Referer": f"https://live.douyin.com/{web_rid}",
                },
                timeout=self._session.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"请求直播间信息失败: {e}")
            raise LiveStreamNotFound(f"无法访问直播间 {web_rid}: {e}")
        except ValueError as e:
            logger.error(f"解析 JSON 失败: {e}")
            raise LiveStreamNotFound(f"直播间 {web_rid} 返回数据无效")

        info = self._parse_room_response(data, web_rid)

        # 生成默认输出路径
        date_str = time.strftime("%Y%m%d")
        ext = ".mp4" if self.output_format == "mp4" else ".flv"
        safe_nick = sanitize_filename(info.nickname or "live", max_length=30)
        safe_title = sanitize_filename(info.title or "", max_length=30)
        filename = f"{date_str}_{safe_nick}_{info.web_rid}"
        if safe_title:
            filename += f"_{safe_title}"
        filename += ext
        self._output_path = f"{self.output_dir}/{filename}"

        return info

    def record(
        self,
        output_path: str | None = None,
        duration: float | None = None,
        on_progress: callable | None = None,
    ) -> str:
        """开始录制直播流。

        持续下载 FLV 流到本地文件，自动刷新过期 URL，
        断线自动重连续写同一文件。

        Args:
            output_path: 输出文件路径，None 则使用 resolve() 自动生成的路径
            duration: 最大录制时长（秒），None=不限时
            on_progress: 进度回调 (bytes_written: int, elapsed: float) -> None

        Returns:
            录制完成的文件路径

        Raises:
            LiveRecordingError: 录制失败
        """
        self._stop_event.clear()
        self._bytes_written = 0
        self._start_time = time.time()
        self._last_url_refresh = time.time()
        self._consecutive_failures = 0

        if not self._current_url:
            raise LiveRecordingError("请先调用 resolve() 获取直播流地址")

        output_path = output_path or self._output_path
        if not output_path:
            raise LiveRecordingError("无法确定输出路径")

        url = self._current_url

        logger.info(f"开始录制 → {output_path}")
        print(f"🔴 开始录制 → {output_path}")
        print(f"   按 Ctrl+C 停止录制")

        try:
            with open(output_path, "wb") as f:
                while not self._stop_event.is_set():
                    # 检查时长限制
                    elapsed = time.time() - self._start_time
                    if duration and elapsed >= duration:
                        print(f"\n⏹ 达到录制时长 {duration:.0f}s，停止")
                        break

                    # 主动刷新即将过期的 URL
                    if time.time() - self._last_url_refresh > URL_REFRESH_INTERVAL:
                        new_url = self._try_refresh_url()
                        if new_url:
                            url = new_url
                            self._last_url_refresh = time.time()
                            self._consecutive_failures = 0
                            continue  # 用新 URL 重连

                    try:
                        resp = self._session._session.get(
                            url,
                            headers={
                                "User-Agent": USERAGENT,
                                "Referer": f"https://live.douyin.com/{self._web_rid}",
                            },
                            stream=True,
                            timeout=30,
                        )
                        resp.raise_for_status()

                        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                            if self._stop_event.is_set():
                                break

                            if chunk:
                                f.write(chunk)
                                self._bytes_written += len(chunk)

                            # 定期检查是否需要刷新 URL
                            if time.time() - self._last_url_refresh > URL_REFRESH_INTERVAL:
                                break

                            # 检查时长限制
                            if duration and (time.time() - self._start_time) >= duration:
                                break

                            # 进度回调（每 ~500ms 调用一次，避免过于频繁）
                            if on_progress and int(elapsed * 2) != int((time.time() - self._start_time) * 2):
                                on_progress(
                                    self._bytes_written,
                                    time.time() - self._start_time,
                                )

                        resp.close()

                    except requests.RequestException as e:
                        logger.warning(f"流连接中断: {e}")
                        if self._stop_event.is_set():
                            break

                        # 等待后重试刷新 URL
                        time.sleep(2)
                        new_url = self._try_refresh_url()
                        if new_url:
                            url = new_url
                            self._last_url_refresh = time.time()
                            self._consecutive_failures = 0
                            logger.info("已重连，继续录制")
                        else:
                            self._consecutive_failures += 1
                            if self._consecutive_failures >= 3:
                                raise LiveRecordingError(
                                    f"连续 {self._consecutive_failures} 次重连失败，录制终止"
                                )
                            time.sleep(3)

        except LiveRecordingError:
            raise
        except KeyboardInterrupt:
            print("\n⏹ 用户中断")
        except Exception as e:
            logger.error(f"录制异常: {e}")
            raise LiveRecordingError(f"录制失败: {e}")

        elapsed = time.time() - self._start_time
        size_mb = self._bytes_written / (1024 * 1024)

        # MP4 格式：FLV 录制完后通过 ffmpeg 转封装
        if self.output_format == "mp4":
            final_path = output_path  # _output_path 已经是 .mp4
            flv_path = output_path.rsplit(".", 1)[0] + ".flv"
            # 当前文件实际存的是 FLV，先改名
            os.rename(output_path, flv_path)
            print(f"🎬 转封装 MP4...")
            remuxed = self._remux_to_mp4(flv_path, final_path)
            if remuxed:
                os.remove(flv_path)
                print(f"⏹ 录制结束: {elapsed:.0f}s / {size_mb:.1f}MB → {final_path}")
                return final_path
            else:
                # FFmpeg 不可用，保留 FLV
                os.rename(flv_path, output_path)
                print(f"⚠ FFmpeg 不可用，保留 FLV 格式")
                print(f"⏹ 录制结束: {elapsed:.0f}s / {size_mb:.1f}MB → {output_path}")
                return output_path
        else:
            print(f"⏹ 录制结束: {elapsed:.0f}s / {size_mb:.1f}MB → {output_path}")
            return output_path

    def stop(self):
        """停止录制。"""
        self._stop_event.set()
        logger.info("收到停止信号")

    @property
    def is_recording(self) -> bool:
        """是否正在录制。"""
        return not self._stop_event.is_set() and self._start_time > 0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def elapsed(self) -> float:
        if self._start_time == 0:
            return 0.0
        return time.time() - self._start_time

    @property
    def output_path(self) -> str:
        return self._output_path

    # ── 内部方法 ──────────────────────────────────────────────

    def _parse_room_response(self, data: dict, web_rid: str) -> LiveStreamInfo:
        """解析 /webcast/room/web/enter/ 响应。

        实际响应结构:
        - data.data[0]: 房间信息 (id_str, status, title, cover, stream_url)
        - data.user: 用户信息 (nickname, sec_uid, avatar_thumb)
        """
        inner = data.get("data", data)

        # 房间信息 — 多种可能位置
        room = {}
        # 路径1: data.data[0] (实测结构)
        room_list = inner.get("data")
        if isinstance(room_list, list) and len(room_list) > 0:
            room = room_list[0]
        # 路径2: data.room / data.room_info (备选)
        if not room:
            room = inner.get("room") or inner.get("room_info") or {}

        status = room.get("status", 0)
        if status != 2:
            raise LiveStreamOffline(
                f"直播间 {web_rid} 未开播 (status={status})"
            )

        # 提取流地址
        stream_urls = self._extract_flv_urls(room)
        if not stream_urls:
            raise LiveStreamOffline(f"直播间 {web_rid} 无可用流地址")

        best_url = self._pick_best_url(stream_urls)

        # 用户信息
        user = inner.get("user") or inner.get("anchor") or inner.get("owner") or {}

        # 观众数
        user_count_str = room.get("user_count_str", "").replace("+", "")
        online_users = 0
        try:
            online_users = int(user_count_str) if user_count_str else 0
        except ValueError:
            online_users = room.get("user_count", 0) or 0

        info = LiveStreamInfo(
            room_id=str(room.get("id_str") or inner.get("enter_room_id", "")),
            web_rid=str(web_rid),
            status=status,
            title=room.get("title", ""),
            nickname=user.get("nickname", ""),
            cover_url=(
                room.get("cover", {}).get("url_list", [""])[0]
                if isinstance(room.get("cover"), dict)
                else ""
            ),
            stream_url=best_url,
            stream_urls=stream_urls,
            online_users=online_users,
        )

        # 缓存当前 URL 用于录制
        self._current_url = best_url
        return info

    @staticmethod
    def _extract_flv_urls(room: dict) -> dict[str, str]:
        """从房间数据中提取所有 FLV 拉流地址。

        尝试多种可能的响应路径:
        - stream_url.flv_pull_url.{quality}
        - stream_url.live_core_sdk_data.pull_data.stream_data (JSON 字符串)
        - stream_url.rtmp_pull_url (作为后备)
        """
        urls = {}
        stream = room.get("stream_url", {})

        if not isinstance(stream, dict):
            return urls

        # 路径1: flv_pull_url 字典（最常见）
        flv_pull = stream.get("flv_pull_url", {})
        if isinstance(flv_pull, dict):
            for quality, url in flv_pull.items():
                if url and isinstance(url, str):
                    urls[quality.upper()] = url

        # 路径2: live_core_sdk_data.pull_data.stream_data
        sdk = stream.get("live_core_sdk_data", {})
        if isinstance(sdk, dict):
            pull = sdk.get("pull_data", {})
            if isinstance(pull, dict):
                stream_data = pull.get("stream_data", {})
                if isinstance(stream_data, dict):
                    for quality, addr in stream_data.items():
                        if isinstance(addr, dict):
                            flv = addr.get("flv") or addr.get("main", {}).get("flv", "")
                            if flv and quality.upper() not in urls:
                                urls[quality.upper()] = flv

        # 路径3: rtmp_pull_url 作为后备
        rtmp = stream.get("rtmp_pull_url", "")
        if rtmp and not urls:
            urls["SD1"] = rtmp

        return urls

    def _pick_best_url(self, urls: dict[str, str]) -> str:
        """按画质优先级选择最佳的 FLV URL。"""
        requested = self.quality.upper()
        if requested in urls:
            return urls[requested]

        for rank in QUALITY_RANK:
            if rank in urls:
                logger.info(f"请求画质 {self.quality} 不可用，降级到 {rank}")
                return urls[rank]

        # 全部不匹配，返回第一个可用
        return next(iter(urls.values()))

    def _try_refresh_url(self) -> str | None:
        """尝试刷新流地址，返回新 URL 或 None。

        静默处理错误以避免干扰录制循环。
        """
        try:
            params = {
                "aid": "6383",
                "app_name": "douyin_web",
                "device_platform": "web",
                "web_rid": self._web_rid,
                "msToken": generate_fake_ms_token(),
            }
            url = f"{API_ROOM_ENTER}?{urlencode(params)}"
            resp = self._session._session.get(
                url,
                headers={
                    "User-Agent": USERAGENT,
                    "Referer": f"https://live.douyin.com/{self._web_rid}",
                },
                timeout=self._session.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            inner = data.get("data", data)

            # 同 _parse_room_response 的逻辑
            room = {}
            room_list = inner.get("data")
            if isinstance(room_list, list) and len(room_list) > 0:
                room = room_list[0]
            if not room:
                room = inner.get("room") or inner.get("room_info") or {}

            status = room.get("status", 0)
            if status != 2:
                logger.info(f"直播已结束 (status={status})")
                return None

            flv_urls = self._extract_flv_urls(room)
            if flv_urls:
                return self._pick_best_url(flv_urls)

        except Exception as e:
            logger.warning(f"刷新流地址失败: {e}")

        return None

    @staticmethod
    def ffmpeg_available() -> bool:
        """检查 FFmpeg 是否可用。"""
        import shutil
        return shutil.which("ffmpeg") is not None

    @staticmethod
    def _remux_to_mp4(flv_path: str, mp4_path: str) -> bool:
        """用 FFmpeg 将 FLV 无损转封装为 MP4。

        -c copy = 不重新编码，只换容器，秒级完成。
        """
        import subprocess

        if not LiveRecorder.ffmpeg_available():
            logger.warning("FFmpeg 不可用，跳过转封装")
            return False

        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", flv_path,
                    "-c", "copy",
                    "-movflags", "+faststart",
                    mp4_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and os.path.exists(mp4_path):
                return True
            else:
                logger.warning(f"FFmpeg 转封装失败: {result.stderr[:200]}")
                return False
        except FileNotFoundError:
            logger.warning("FFmpeg 不可用")
            return False
        except Exception as e:
            logger.warning(f"转封装异常: {e}")
            return False
