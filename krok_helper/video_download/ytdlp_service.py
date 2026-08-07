from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from krok_helper.ffmpeg import find_tool
from krok_helper.network import (
    build_urllib_opener_for_app_settings,
    load_current_app_settings,
    proxy_cli_args_for_app_settings,
    proxy_url_for_app_settings,
    subprocess_env_for_app_settings,
)

from .download_task import (
    DownloadOptions,
    DownloadTask,
    NAMING_RULE_CUSTOM,
    NAMING_RULE_TITLE,
    NAMING_RULE_TITLE_UPLOADER,
    SOURCE_BILIBILI,
    SOURCE_UNKNOWN,
    SOURCE_YOUTUBE,
    VideoInfo,
)
from .format_parser import FormatParser


WINDOWS_INVALID_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|]+')
YOUTUBE_FALLBACK_EXTRACTOR_ARGS = "youtube:player_client=android_vr,web"
YOUTUBE_DISABLE_COOKIE_HINT = "no_cookie"
YOUTUBE_HINT_SEPARATOR = "|"

# ── B 站海外线路的抗断流参数 ───────────────────────────────────────────────
# B 站分给海外客户端的 upos CDN 会间歇性卡死单条 TCP 连接。整段视频只用一条连接
# 顺序拉时，那条连接一死整个下载就死——用户侧表现为"速度突然掉到 0 → 断连 → 失败"。
# 实测同一个 URL、同一个 host：单连接卡死在 4MB 处超时，8 条 Range 并发则完成
# （8 条里有 2~3 条同样卡死，但其余照常跑完）。BBDown 之所以稳，正是因为它默认
# 多线程分块下载 + 整体失败自动重试。
#
# 下面按优先级两档兜底：
#   aria2c 可用   → 多连接下载，既抗断流又提速（对齐 BBDown 的 -mt）
#   aria2c 不可用 → HTTP 分块 Range，仍是单连接但卡死只损失一个分块且能单独重试
BILIBILI_HTTP_CHUNK_SIZE = 4 * 1024 * 1024
# 界面上的"重试次数"是任务级语义（用户理解成"整体重试几次"），不该被直接当作
# yt-dlp 的 HTTP 级重试上限——抖动线路上 3 次太少。yt-dlp 自己的默认就是 10。
MIN_HTTP_RETRIES = 10
# 这些参数排在 yt-dlp 自带的 ``-x16 -j16 -s16`` 之后，同名项以这里为准。
# 注意别加 ``--lowest-speed-limit``：实测它会把"慢但活着"的连接一并掐掉，而 aria2
# 不会重新补上分片，连接数从 16 一路掉到 1，尾段反而更慢。只掐真正卡死的连接
# （``--timeout`` 读超时），慢连接留着继续跑。
ARIA2C_DOWNLOAD_ARGS = (
    "--max-tries=10",
    "--retry-wait=2",
    "--connect-timeout=10",
    "--timeout=10",
    # aria2 默认走 c-ares 自己做 DNS，会绕开系统的地址选择策略（RFC 6724）：
    # 没有 IPv6 出口的机器照样会拿到 akamaized.net 的 AAAA 记录并去连，直接
    # WSAENETUNREACH "unreachable network" 报错退出（B 站的音频流经常落在
    # akamai 上，所以很容易踩到）。交给系统解析器就和 yt-dlp 自身行为一致了；
    # 注意别用 --disable-ipv6，那会把真有 IPv6 的用户也一起断掉。
    "--async-dns=false",
    # 控制文件默认 60 秒才落盘一次，进度条会一分钟才动一下。
    # _read_aria2_control 靠它算真实进度，所以压到 1 秒。
    "--auto-save-interval=1",
    # 无头 GUI 里没有可写的 stdout，关掉进度回显（进度由 _start_part_progress_watcher 提供）
    "--show-console-readout=false",
    "--summary-interval=0",
    "--console-log-level=error",
)
# yt-dlp 走外部下载器时只在结束时回调一次 progress_hook，进度条会整段卡住不动。
# 轮询 ``.part`` 文件大小把进度补回来。
ARIA2C_PROGRESS_POLL_SECONDS = 0.5
PART_SUFFIX = ".part"
ARIA2C_CONTROL_SUFFIX = ".aria2"

# ── 下载完成后的完整性校验 ────────────────────────────────────────────────
# 抖动线路上出现过「yt-dlp 报 100%、合并也没报错，但产出的视频轨只有 10 秒
# （音轨 240 秒）」这种静默损坏。这类文件比一个干脆的失败危险得多——它会被
# 当成正常素材喂给后面的对齐/渲染步骤。
#
# 注意必须逐流核对：上面那个坏文件的**容器**时长是 240 秒（跟着音轨走），
# 只看 format.duration 根本发现不了，得看 video 流自己的 duration。
DURATION_TOLERANCE_RATIO = 0.15
MIN_DURATION_TOLERANCE_SECONDS = 3.0
FFPROBE_TIMEOUT_SECONDS = 60
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9;]*m")


class VideoDownloadError(RuntimeError):
    """Raised when yt-dlp operations fail."""


class DownloadCancelledError(VideoDownloadError):
    """Raised when the user cancels the current download."""


class _QuietYtDlpLogger:
    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


class YtDlpService:
    def __init__(self, format_parser: FormatParser | None = None, app_settings=None) -> None:
        self._format_parser = format_parser or FormatParser()
        self._app_settings = app_settings
        self._cli_version_cache: dict[str, str] = {}
        self._cli_path_cache = ""
        self._aria2c_path_cache: str | None = None

    def _settings(self):
        return self._app_settings or load_current_app_settings()

    def _configured_ffmpeg_location(self) -> str:
        value = str(getattr(self._settings(), "ffmpeg_dir", "") or "").strip()
        if not value or value == ".":
            return ""
        return find_tool("ffmpeg", Path(value).expanduser())

    # ── 抗断流：重试 / 分块 / 多连接 ────────────────────────────────────────
    def _http_retries(self, options: DownloadOptions) -> int:
        return max(int(options.retry_count), MIN_HTTP_RETRIES)

    def find_aria2c(self) -> str:
        """返回可用的 aria2c 路径；找不到返回空串。结果缓存在实例上。

        查找顺序：随包分发的副本 > 系统 PATH。打包版优先用自带的那份——版本
        确定、参数行为可预期；用户 PATH 上那个可能是很老的构建。源码运行时没有
        随包副本，自然落到 PATH。
        """
        if self._aria2c_path_cache is not None:
            return self._aria2c_path_cache

        names = ("aria2c.exe",) if os.name == "nt" else ("aria2c",)
        for base, subdir in self._aria2c_search_locations():
            for name in names:
                candidate = base.joinpath(*subdir, name)
                if candidate.is_file():
                    self._aria2c_path_cache = str(candidate.resolve())
                    return self._aria2c_path_cache

        resolved = shutil.which("aria2c")
        self._aria2c_path_cache = str(Path(resolved).resolve()) if resolved else ""
        return self._aria2c_path_cache

    def _aria2c_search_locations(self) -> list[tuple[Path, tuple[str, ...]]]:
        """(根目录, 子路径) 列表，按优先级排列。"""
        bases: list[Path] = []
        # 打包版：PyInstaller onedir 把 --add-binary 的内容放在 _internal/ 下，
        # 也就是 sys._MEIPASS。这一份属于 runtime part（见 scripts/build_parts.py）。
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bases.append(Path(meipass))
        for candidate_base in (Path(sys.executable).parent, Path(__file__).resolve().parents[2]):
            try:
                bases.append(candidate_base.resolve())
            except OSError:
                continue

        locations: list[tuple[Path, tuple[str, ...]]] = []
        for base in dict.fromkeys(bases):
            locations.append((base, ("tools", "aria2")))
            # 源码运行时用 scripts/fetch_aria2.py 的产出目录，省得开发机再单独装一份
            locations.append((base, ("build", "vendor", "aria2")))
        return locations

    def _use_aria2c_for(self, url: str, options: DownloadOptions) -> str:
        # 只对 B 站开：YouTube 走 yt-dlp 自己的分片下载器已经是多连接的，
        # 换成 aria2c 反而更容易触发限速。
        if self.detect_source(url) != SOURCE_BILIBILI:
            return ""
        if not getattr(options, "use_aria2c", True):
            return ""
        return self.find_aria2c()

    def _download_resilience_opts(self, url: str, options: DownloadOptions) -> dict[str, Any]:
        """Python API 侧的抗断流选项。"""
        retries = self._http_retries(options)
        opts: dict[str, Any] = {
            "retries": retries,
            "fragment_retries": retries,
            "socket_timeout": max(1, int(options.timeout)),
        }
        aria2c = self._use_aria2c_for(url, options)
        if aria2c:
            opts["external_downloader"] = {"default": aria2c}
            opts["external_downloader_args"] = {"aria2c": list(ARIA2C_DOWNLOAD_ARGS)}
        elif self.detect_source(url) == SOURCE_BILIBILI:
            opts["http_chunk_size"] = BILIBILI_HTTP_CHUNK_SIZE
        return opts

    def _should_retry_without_aria2c(self, url: str, options: DownloadOptions, message: str) -> bool:
        """aria2c 起不来时降级到内置分块下载器重试一次。

        aria2c 失败的原因往往和视频本身无关（没有 IPv6 出口、被杀软拦、二进制损坏
        等等），此时内置分块下载器多半是能跑通的——手上有可用退路就不该让用户直接
        看到一个硬失败。
        """
        if not self._use_aria2c_for(url, options):
            return False
        lower = ANSI_ESCAPE_PATTERN.sub("", message).lower()
        return "aria2c" in lower

    def _download_resilience_cli_args(self, url: str, options: DownloadOptions) -> list[str]:
        """CLI 侧的等价参数。"""
        retries = str(self._http_retries(options))
        args = [
            "--retries",
            retries,
            "--fragment-retries",
            retries,
            "--socket-timeout",
            str(max(1, int(options.timeout))),
        ]
        aria2c = self._use_aria2c_for(url, options)
        if aria2c:
            args.extend(["--downloader", aria2c])
            args.extend(["--downloader-args", f"aria2c:{' '.join(ARIA2C_DOWNLOAD_ARGS)}"])
        elif self.detect_source(url) == SOURCE_BILIBILI:
            args.extend(["--http-chunk-size", str(BILIBILI_HTTP_CHUNK_SIZE)])
        return args

    def get_ytdlp_version(self) -> str:
        try:
            import yt_dlp
        except ModuleNotFoundError:
            cli = self._find_ytdlp_cli()
            completed = subprocess.run(
                [cli, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=15,
                env=subprocess_env_for_app_settings(self._settings()),
                creationflags=self._subprocess_creationflags(),
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip() or "无法读取 yt-dlp 版本。"
                raise VideoDownloadError(message)
            version = completed.stdout.strip() or "未知版本"
            return f"命令行版 {version}"
        return f"Python 包 {yt_dlp.version.__version__}"

    def get_latest_ytdlp_version(self) -> str:
        request = urllib.request.Request(
            "https://pypi.org/pypi/yt-dlp/json",
            headers={"User-Agent": "krok-helper"},
        )
        try:
            with build_urllib_opener_for_app_settings(self._settings()).open(request, timeout=15) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(f"无法查询 yt-dlp 最新版本：{exc}") from exc
        version = str((payload.get("info") or {}).get("version") or "").strip()
        if not version:
            raise VideoDownloadError("PyPI 未返回 yt-dlp 最新版本。")
        return version

    def update_ytdlp(self) -> str:
        # PyInstaller frozen 包里 ``sys.executable`` 是宿主 ``Karaoke Studio.exe``，
        # 不是 python.exe；走 ``<app>.exe -m pip install -U yt-dlp`` 只会触发主程序的
        # argparse 退路，把 Karaoke Studio 自己的 usage 当成 yt-dlp 的更新输出回填到
        # 状态栏（v3.0.6 之前的真实事故）。而且 frozen bundle 里 yt_dlp 是只读烧进
        # ``_internal/`` 的，pip 即便能跑也写不进去——所以打包版不应该尝试 pip 路径，
        # 只去用户系统 PATH 上可能存在的独立 yt-dlp CLI；找不到就由 ``_update_ytdlp_cli``
        # 抛清晰的中文错误。
        if getattr(sys, "frozen", False):
            return self._update_ytdlp_cli()
        try:
            import yt_dlp  # noqa: F401
        except ModuleNotFoundError:
            return self._update_ytdlp_cli()
        return self._update_ytdlp_python_package()

    def extract_info(self, url: str, cookie_file: str | None = None) -> VideoInfo:
        infos = self.extract_infos(url, cookie_file)
        if not infos:
            raise VideoDownloadError("没有可用的解析结果。")
        return infos[0]

    def extract_infos(self, url: str, cookie_file: str | None = None) -> list[VideoInfo]:
        source = self.detect_source(url)
        raw_info, extractor_args_hint = self._extract_info_with_best_backend(
            url,
            cookie_file,
            allow_playlist=source == SOURCE_BILIBILI,
        )
        entries = self._unwrap_bilibili_entries(raw_info) if source == SOURCE_BILIBILI else []
        if entries:
            parent_title = str(raw_info.get("title") or "")
            total = len(entries)
            return [
                self._build_video_info(
                    self._hydrate_playlist_entry(entry, cookie_file),
                    url,
                    extractor_args_hint,
                    parent_title=parent_title,
                    part_index=index + 1,
                    part_total=total,
                )
                for index, entry in enumerate(entries)
            ]
        return [self._build_video_info(self._unwrap_info(raw_info), url, extractor_args_hint)]

    def _hydrate_playlist_entry(
        self,
        entry: dict[str, Any],
        cookie_file: str | None,
    ) -> dict[str, Any]:
        if entry.get("formats"):
            return entry
        entry_url = self._coerce_webpage_url(entry, "")
        if not entry_url:
            return entry
        try:
            raw_entry, _hint = self._extract_info_with_best_backend(
                entry_url,
                cookie_file,
                allow_playlist=False,
            )
        except Exception:
            return entry
        return self._unwrap_info(raw_entry)

    def _build_video_info(
        self,
        info: dict[str, Any],
        fallback_url: str,
        extractor_args_hint: str,
        *,
        parent_title: str = "",
        part_index: int = 0,
        part_total: int = 0,
    ) -> VideoInfo:
        source = self.detect_source(fallback_url, info.get("extractor_key"))
        preferred_audio_ext = "m4a" if source == SOURCE_YOUTUBE else ""
        formats = self._format_parser.parse_formats(info.get("formats"), preferred_audio_ext=preferred_audio_ext)
        thumbnail_url = str(info.get("thumbnail") or "")
        webpage_url = self._coerce_webpage_url(info, fallback_url)
        title = str(info.get("title") or "未命名视频")
        if parent_title and title and title != parent_title:
            prefix = f"P{part_index} " if part_total > 1 and part_index > 0 else ""
            title = f"{parent_title} - {prefix}{title}".strip()
        return VideoInfo(
            url=webpage_url,
            source=source,
            title=title,
            uploader=str(info.get("uploader") or info.get("channel") or info.get("owner") or "-"),
            duration=float(info["duration"]) if info.get("duration") else None,
            thumbnail_url=thumbnail_url,
            thumbnail_bytes=self._fetch_thumbnail_bytes(thumbnail_url),
            webpage_url=webpage_url,
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            filesize=self._pick_filesize(info),
            formats=formats,
            recommended_option_id=formats[0].option_id if formats else "",
            subtitles_available=bool(info.get("subtitles") or info.get("automatic_captions")),
            extractor_args_hint=extractor_args_hint,
        )

    def download(
        self,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        save_dir = Path(options.save_dir).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)
        title = task.title or (task.info.title if task.info else "未命名视频")
        uploader = task.info.uploader if task.info else ""
        resolution = task.selected_format.resolution if task.selected_format else ""
        if not resolution and task.info and task.info.height:
            resolution = f"{task.info.height}p"
        output_stem = self._build_output_stem(
            title=title,
            uploader=uploader,
            resolution=resolution,
            options=options,
        )
        outtmpl = str(save_dir / f"{output_stem}.%(ext)s")
        selected_format = task.selected_format.download_format if task.selected_format else "best"
        extractor_args_hint = task.info.extractor_args_hint if task.info else ""
        preexisting_outputs = self._snapshot_output_candidates(save_dir, output_stem)

        youtube_dl = self._import_ytdlp()
        use_python_backend = youtube_dl is not None and not self._should_prefer_cli_backend(task.url)
        try:
            if use_python_backend:
                self._download_with_python_retry(
                    youtube_dl,
                    task,
                    options,
                    progress_callback,
                    save_dir=save_dir,
                    output_stem=output_stem,
                    outtmpl=outtmpl,
                    selected_format=selected_format,
                    extractor_args_hint=extractor_args_hint,
                )
            else:
                self._download_with_cli_retry(
                    task,
                    options,
                    progress_callback,
                    save_dir=save_dir,
                    output_stem=output_stem,
                    outtmpl=outtmpl,
                    selected_format=selected_format,
                    extractor_args_hint=extractor_args_hint,
                )
        except DownloadCancelledError:
            self._cleanup_cancelled_outputs(save_dir, output_stem, preexisting_outputs)
            raise

        self._verify_downloaded_media(task)

    def _extract_info_with_best_backend(
        self,
        url: str,
        cookie_file: str | None = None,
        *,
        allow_playlist: bool = False,
    ) -> tuple[dict[str, Any], str]:
        if self._should_prefer_cli_backend(url):
            return self._extract_info_with_cli_retry(url, cookie_file, allow_playlist=allow_playlist)
        youtube_dl = self._import_ytdlp()
        if youtube_dl is not None:
            return self._extract_info_with_python_retry(youtube_dl, url, cookie_file, allow_playlist=allow_playlist)
        return self._extract_info_with_cli_retry(url, cookie_file, allow_playlist=allow_playlist)

    def _extract_info_with_python_retry(
        self,
        youtube_dl,
        url: str,
        cookie_file: str | None,
        *,
        allow_playlist: bool = False,
    ) -> tuple[dict[str, Any], str]:
        try:
            return self._extract_info_with_python_api(youtube_dl, url, cookie_file, allow_playlist=allow_playlist), ""
        except VideoDownloadError as exc:
            if self._should_retry_youtube_with_fallback(url, str(exc)):
                try:
                    return (
                        self._extract_info_with_python_api(
                            youtube_dl,
                            url,
                            cookie_file,
                            extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                            allow_playlist=allow_playlist,
                        ),
                        YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                    )
                except VideoDownloadError as fallback_exc:
                    if self._usable_cookie_file(cookie_file) and self._should_retry_youtube_with_fallback(
                        url,
                        str(fallback_exc),
                    ):
                        no_cookie_hint = self._with_no_cookie_hint(YOUTUBE_FALLBACK_EXTRACTOR_ARGS)
                        return (
                            self._extract_info_with_python_api(
                                youtube_dl,
                                url,
                                None,
                                extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                                allow_playlist=allow_playlist,
                            ),
                            no_cookie_hint,
                        )
                    raise
            if self._usable_cookie_file(cookie_file) and self._should_retry_youtube_without_cookies(url, str(exc)):
                return (
                    self._extract_info_with_python_api(
                        youtube_dl,
                        url,
                        None,
                        extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                        allow_playlist=allow_playlist,
                    ),
                    self._with_no_cookie_hint(YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
                )
            raise

    def _extract_info_with_python_api(
        self,
        youtube_dl,
        url: str,
        cookie_file: str | None,
        *,
        extractor_args_hint: str = "",
        allow_playlist: bool = False,
    ) -> dict[str, Any]:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": not allow_playlist,
            "skip_download": True,
            "logger": _QuietYtDlpLogger(),
        }
        usable_cookie_file = "" if self._hint_disables_cookies(extractor_args_hint) else self._usable_cookie_file(cookie_file)
        if usable_cookie_file:
            ydl_opts["cookiefile"] = usable_cookie_file
        if extractor_args_hint:
            ydl_opts["extractor_args"] = self._build_python_extractor_args(extractor_args_hint)
        proxy_url = proxy_url_for_app_settings(self._settings())
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        try:
            with youtube_dl(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc

    def _extract_info_with_cli_retry(
        self,
        url: str,
        cookie_file: str | None,
        *,
        allow_playlist: bool = False,
    ) -> tuple[dict[str, Any], str]:
        try:
            return self._extract_info_with_cli(url, cookie_file, allow_playlist=allow_playlist), ""
        except VideoDownloadError as exc:
            if self._should_retry_youtube_with_fallback(url, str(exc)):
                try:
                    return (
                        self._extract_info_with_cli(
                            url,
                            cookie_file,
                            extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                            allow_playlist=allow_playlist,
                        ),
                        YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                    )
                except VideoDownloadError as fallback_exc:
                    if self._usable_cookie_file(cookie_file) and self._should_retry_youtube_with_fallback(
                        url,
                        str(fallback_exc),
                    ):
                        no_cookie_hint = self._with_no_cookie_hint(YOUTUBE_FALLBACK_EXTRACTOR_ARGS)
                        return (
                            self._extract_info_with_cli(
                                url,
                                None,
                                extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                                allow_playlist=allow_playlist,
                            ),
                            no_cookie_hint,
                        )
                    raise
            if self._usable_cookie_file(cookie_file) and self._should_retry_youtube_without_cookies(url, str(exc)):
                return (
                    self._extract_info_with_cli(
                        url,
                        None,
                        extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                        allow_playlist=allow_playlist,
                    ),
                    self._with_no_cookie_hint(YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
                )
            raise

    def _extract_info_with_cli(
        self,
        url: str,
        cookie_file: str | None = None,
        *,
        extractor_args_hint: str = "",
        allow_playlist: bool = False,
    ) -> dict[str, Any]:
        command = [
            self._find_ytdlp_cli(),
            "--dump-single-json",
            "--skip-download",
            "--yes-playlist" if allow_playlist else "--no-playlist",
            "--no-warnings",
            "--no-update",
            url,
        ]
        stripped_extractor_args_hint = self._strip_hint_flags(extractor_args_hint)
        if stripped_extractor_args_hint:
            command[1:1] = ["--extractor-args", stripped_extractor_args_hint]
        usable_cookie_file = self._usable_cookie_file(cookie_file)
        if usable_cookie_file:
            command[1:1] = ["--cookies", usable_cookie_file]
        proxy_args = proxy_cli_args_for_app_settings(self._settings())
        if proxy_args:
            command[1:1] = proxy_args

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=60,
                env=subprocess_env_for_app_settings(self._settings()),
                creationflags=self._subprocess_creationflags(),
            )
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "yt-dlp failed"
            raise VideoDownloadError(self._normalize_error_message(RuntimeError(message)))

        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise VideoDownloadError("yt-dlp 返回了无法解析的 JSON 结果。") from exc

    def _download_with_python_retry(
        self,
        youtube_dl,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
        *,
        save_dir: Path,
        output_stem: str,
        outtmpl: str,
        selected_format: str,
        extractor_args_hint: str,
    ) -> None:
        try:
            self._download_with_python_api(
                youtube_dl,
                task,
                options,
                progress_callback,
                save_dir=save_dir,
                output_stem=output_stem,
                outtmpl=outtmpl,
                selected_format=selected_format,
                extractor_args_hint=extractor_args_hint,
            )
        except VideoDownloadError as exc:
            if self._should_retry_youtube_with_fallback(task.url, str(exc), extractor_args_hint):
                self._download_with_python_api(
                    youtube_dl,
                    task,
                    options,
                    progress_callback,
                    save_dir=save_dir,
                    output_stem=output_stem,
                    outtmpl=outtmpl,
                    selected_format=selected_format,
                    extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                )
                return
            if self._should_retry_without_aria2c(task.url, options, str(exc)):
                self._clear_partial_downloads(save_dir, output_stem)
                self._download_with_python_api(
                    youtube_dl,
                    task,
                    replace(options, use_aria2c=False),
                    progress_callback,
                    save_dir=save_dir,
                    output_stem=output_stem,
                    outtmpl=outtmpl,
                    selected_format=selected_format,
                    extractor_args_hint=extractor_args_hint,
                )
                return
            raise

    def _download_with_python_api(
        self,
        youtube_dl,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
        *,
        save_dir: Path,
        output_stem: str,
        outtmpl: str,
        selected_format: str,
        extractor_args_hint: str = "",
    ) -> None:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": selected_format,
            "overwrites": True,
            "progress_hooks": [self._build_hook(task, progress_callback)],
            "writethumbnail": bool(options.download_thumbnail),
            "writesubtitles": bool(options.download_subtitle),
            "writeautomaticsub": bool(options.download_subtitle),
        }
        ydl_opts.update(self._download_resilience_opts(task.url, options))
        if extractor_args_hint:
            ydl_opts["extractor_args"] = self._build_python_extractor_args(extractor_args_hint)
        if options.merge_video_audio:
            ydl_opts["merge_output_format"] = "mp4"
        ffmpeg_location = self._configured_ffmpeg_location()
        if ffmpeg_location:
            ydl_opts["ffmpeg_location"] = ffmpeg_location
        usable_cookie_file = "" if self._hint_disables_cookies(extractor_args_hint) else self._usable_cookie_file(options.cookie_file)
        if usable_cookie_file:
            ydl_opts["cookiefile"] = usable_cookie_file
        proxy_url = proxy_url_for_app_settings(self._settings())
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        before_pids = self._snapshot_child_pids()
        done_event = threading.Event()
        watcher = self._start_cancel_watcher(task, done_event, before_pids)
        # 外部下载器不回调逐字节进度，靠轮询 .part 把进度条喂活
        progress_watcher = (
            self._start_part_progress_watcher(save_dir, output_stem, done_event, progress_callback)
            if "external_downloader" in ydl_opts
            else None
        )
        try:
            with youtube_dl(ydl_opts) as ydl:
                result = ydl.extract_info(task.url, download=True)
                final_info = self._unwrap_info(result)
                task.local_file = self._resolve_output_file(save_dir, output_stem, final_info, task, options)
        except DownloadCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if task.cancel_requested:
                raise DownloadCancelledError("下载已取消。") from exc
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc
        finally:
            done_event.set()
            watcher.join(timeout=1)
            if progress_watcher is not None:
                progress_watcher.join(timeout=1)

    def _download_with_cli_retry(
        self,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
        *,
        save_dir: Path,
        output_stem: str,
        outtmpl: str,
        selected_format: str,
        extractor_args_hint: str,
    ) -> None:
        try:
            self._download_with_cli(
                task,
                options,
                progress_callback,
                save_dir=save_dir,
                output_stem=output_stem,
                outtmpl=outtmpl,
                selected_format=selected_format,
                extractor_args_hint=extractor_args_hint,
            )
        except VideoDownloadError as exc:
            if self._should_retry_youtube_with_fallback(task.url, str(exc), extractor_args_hint):
                self._download_with_cli(
                    task,
                    options,
                    progress_callback,
                    save_dir=save_dir,
                    output_stem=output_stem,
                    outtmpl=outtmpl,
                    selected_format=selected_format,
                    extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
                )
                return
            raise

    def _download_with_cli(
        self,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
        *,
        save_dir: Path,
        output_stem: str,
        outtmpl: str,
        selected_format: str,
        extractor_args_hint: str = "",
    ) -> None:
        progress_marker = "__KROK_PROGRESS__"
        command = [
            self._find_ytdlp_cli(),
            "--newline",
            "--no-warnings",
            "--no-update",
            "--no-playlist",
            "--force-overwrites",
            "--output",
            outtmpl,
            "--format",
            selected_format,
            *self._download_resilience_cli_args(task.url, options),
            "--progress-template",
            (
                f"download:{progress_marker}"
                "%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
                "%(progress.fragment_index)s|%(progress.fragment_count)s"
            ),
        ]
        stripped_extractor_args_hint = self._strip_hint_flags(extractor_args_hint)
        if stripped_extractor_args_hint:
            command.extend(["--extractor-args", stripped_extractor_args_hint])
        if options.download_thumbnail:
            command.append("--write-thumbnail")
        if options.download_subtitle:
            command.extend(["--write-subs", "--write-auto-subs"])
        if options.merge_video_audio:
            command.extend(["--merge-output-format", "mp4"])
        ffmpeg_location = self._configured_ffmpeg_location()
        if ffmpeg_location:
            command.extend(["--ffmpeg-location", ffmpeg_location])
        usable_cookie_file = "" if self._hint_disables_cookies(extractor_args_hint) else self._usable_cookie_file(options.cookie_file)
        if usable_cookie_file:
            command.extend(["--cookies", usable_cookie_file])
        command.extend(proxy_cli_args_for_app_settings(self._settings()))
        command.append(task.url)

        before_pids = self._snapshot_child_pids()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=subprocess_env_for_app_settings(self._settings()),
            creationflags=self._subprocess_creationflags(),
        )
        done_event = threading.Event()
        watcher = self._start_cancel_watcher(task, done_event, before_pids, process=process)
        output_lines: list[str] = []

        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                output_lines.append(line)
                if self._is_cli_merge_line(line):
                    self._emit_merge_progress(progress_callback=progress_callback)
                if task.cancel_requested and process.poll() is None:
                    process.terminate()
                    raise DownloadCancelledError("下载已取消。")
                if progress_marker in line:
                    marker_index = line.index(progress_marker) + len(progress_marker)
                    self._emit_cli_progress(line[marker_index:], progress_callback=progress_callback)

            return_code = process.wait()
            if task.cancel_requested:
                raise DownloadCancelledError("下载已取消。")
        except DownloadCancelledError:
            self._terminate_process(process)
            raise
        except Exception as exc:  # noqa: BLE001
            self._terminate_process(process)
            if task.cancel_requested:
                raise DownloadCancelledError("下载已取消。") from exc
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc
        finally:
            done_event.set()
            watcher.join(timeout=1)

        if return_code != 0:
            if task.cancel_requested:
                raise DownloadCancelledError("下载已取消。")
            resolved_file = self._resolve_output_file(save_dir, output_stem, {}, task, options)
            if resolved_file is not None and resolved_file.is_file() and resolved_file.stat().st_size > 0:
                task.local_file = resolved_file
                return
            message = self._pick_cli_error_message(output_lines, progress_marker)
            raise VideoDownloadError(self._normalize_error_message(RuntimeError(message)))

        task.local_file = self._resolve_output_file(save_dir, output_stem, {}, task, options)

    def _pick_cli_error_message(self, output_lines: list[str], progress_marker: str) -> str:
        meaningful_lines = [
            line
            for line in output_lines
            if line and progress_marker not in line and not line.startswith("[download]")
        ]
        error_line = next((line for line in reversed(meaningful_lines) if "ERROR:" in line), "")
        if error_line:
            return error_line
        warning_line = next((line for line in reversed(meaningful_lines) if "WARNING:" in line), "")
        if warning_line:
            return warning_line
        return next(reversed(meaningful_lines), "yt-dlp download failed")

    def detect_source(self, url: str, extractor_key: str | None = None) -> str:
        normalized = url.lower()
        extractor_key = (extractor_key or "").lower()
        if "bilibili" in normalized or "bili" in extractor_key:
            return SOURCE_BILIBILI
        if "youtube.com" in normalized or "youtu.be" in normalized or "youtube" in extractor_key:
            return SOURCE_YOUTUBE
        return SOURCE_UNKNOWN

    def _import_ytdlp(self):
        try:
            from yt_dlp import YoutubeDL
        except ModuleNotFoundError:
            return None
        return YoutubeDL

    def _should_prefer_cli_backend(self, url: str) -> bool:
        if self.detect_source(url) != SOURCE_YOUTUBE:
            return False
        cli = self._find_ytdlp_cli_or_none()
        if not cli:
            return False
        python_version = self._python_ytdlp_version()
        if not python_version:
            return True
        cli_version = self._read_ytdlp_cli_version(cli)
        return self._version_key(cli_version) > self._version_key(python_version)

    def _python_ytdlp_version(self) -> str:
        try:
            import yt_dlp
        except ModuleNotFoundError:
            return ""
        return str(getattr(yt_dlp.version, "__version__", "") or "")

    def _read_ytdlp_cli_version(self, cli: str) -> str:
        if cli in self._cli_version_cache:
            return self._cli_version_cache[cli]
        try:
            completed = subprocess.run(
                [cli, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=15,
                env=subprocess_env_for_app_settings(self._settings()),
                creationflags=self._subprocess_creationflags(),
            )
        except Exception:
            return ""
        if completed.returncode != 0:
            return ""
        version = completed.stdout.strip()
        self._cli_version_cache[cli] = version
        return version

    def _version_key(self, version: str) -> tuple[int, ...]:
        match = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?", version)
        if not match:
            return ()
        return tuple(int(part) for part in match.groups(default="0"))

    def _find_ytdlp_cli(self) -> str:
        cli = self._find_ytdlp_cli_or_none()
        if cli:
            return cli
        # 打包版用户没装独立 yt-dlp 时给出更准确的引导——pip / Python 包对他们没用
        if getattr(sys, "frozen", False):
            raise VideoDownloadError(
                "未在系统 PATH 上找到独立的 yt-dlp CLI。"
                "打包版的 Karaoke Studio 内置 yt-dlp 是只读的，无法热更新；"
                "请整体升级应用，或者单独安装 yt-dlp 到系统 PATH 后再点更新。"
            )
        raise VideoDownloadError("未找到 yt-dlp。请安装 `yt-dlp` 命令或 Python 包。")

    def _find_ytdlp_cli_or_none(self) -> str:
        if self._cli_path_cache:
            return self._cli_path_cache
        candidates: list[Path] = []
        cli = shutil.which("yt-dlp")
        if cli:
            candidates.append(Path(cli))
        names = ("yt-dlp.exe", "yt-dlp") if os.name == "nt" else ("yt-dlp",)
        bases: list[Path] = []
        try:
            bases.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
        try:
            bases.append(Path.cwd().resolve())
        except OSError:
            pass
        try:
            bases.extend(Path(__file__).resolve().parents[:4])
        except OSError:
            pass
        for base in dict.fromkeys(bases):
            for name in names:
                candidate = base / name
                if candidate.is_file():
                    candidates.append(candidate)
        unique_candidates = [path for path in dict.fromkeys(str(path.resolve()) for path in candidates)]
        if not unique_candidates:
            return ""
        self._cli_path_cache = max(
            unique_candidates,
            key=lambda path: self._version_key(self._read_ytdlp_cli_version(path)),
        )
        return self._cli_path_cache

    def _usable_cookie_file(self, cookie_file: str | None) -> str:
        if not cookie_file:
            return ""
        path = Path(cookie_file)
        if not path.is_file() or path.stat().st_size <= 0:
            return ""
        jar = http.cookiejar.MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            return ""
        if not any(True for _cookie in jar):
            return ""
        return str(path)

    def _update_ytdlp_cli(self) -> str:
        cli = self._find_ytdlp_cli()
        try:
            return self._run_update_command([cli, *proxy_cli_args_for_app_settings(self._settings()), "-U"])
        except VideoDownloadError as exc:
            message = str(exc)
            if self._should_fallback_to_pip_update(message):
                python_executable = self._python_executable_for_cli(cli)
                pip_output = self._update_ytdlp_python_package(python_executable)
                return (
                    "yt-dlp 命令行自更新不可用，已改用对应 Python 环境的 pip 更新 yt-dlp。\n"
                    f"{pip_output}"
                )
            raise

    def _update_ytdlp_python_package(self, python_executable: str | None = None) -> str:
        python = python_executable or sys.executable
        return self._run_update_command([python, "-m", "pip", "install", "-U", "yt-dlp"])

    def _python_executable_for_cli(self, cli: str) -> str:
        cli_path = Path(cli)
        if os.name == "nt":
            parent = cli_path.parent
            if parent.name.lower() == "scripts":
                candidate = parent.parent / "python.exe"
                if candidate.is_file():
                    return str(candidate)
        # frozen 模式下不能回退到 sys.executable —— 那是宿主 .exe，不是 Python，
        # 拿去跑 ``-m pip install`` 会复现 update_ytdlp 头部注释里那个 v3.0.6 之前
        # 的事故。明确抛出让上层 fallback 链断在这里，由 ``_update_ytdlp_cli`` 把
        # 原始 CLI 错误传给用户。
        if getattr(sys, "frozen", False):
            raise VideoDownloadError(
                "打包版无法定位独立的 Python 解释器来跑 pip 更新 yt-dlp。"
                "请整体升级 Karaoke Studio，或者单独安装 yt-dlp CLI 到系统 PATH 后再试。"
            )
        return sys.executable

    def normalize_version(self, version_text: str) -> str:
        match = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?", version_text)
        if not match:
            return version_text.strip()
        year, month, day, suffix = match.groups()
        normalized = f"{int(year):04d}.{int(month):02d}.{int(day):02d}"
        if suffix is not None:
            normalized = f"{normalized}.{suffix}"
        return normalized

    def _should_fallback_to_pip_update(self, message: str) -> bool:
        lower = message.lower()
        return (
            "installed yt-dlp with pip" in lower
            or "wheel from pypi" in lower
            or "use that to update" in lower
        )

    def _run_update_command(self, command: list[str]) -> str:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=180,
                env=subprocess_env_for_app_settings(self._settings()),
                creationflags=self._subprocess_creationflags(),
            )
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc

        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        if completed.returncode != 0:
            raise VideoDownloadError(output or "yt-dlp 更新失败。")
        return output or "yt-dlp 已更新。"

    def _unwrap_info(self, raw_info: dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_info, dict) and raw_info.get("entries"):
            entries = [entry for entry in raw_info.get("entries") or [] if isinstance(entry, dict)]
            if entries:
                return entries[0]
        return raw_info

    def _unwrap_bilibili_entries(self, raw_info: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw_info, dict):
            return []
        entries = [entry for entry in raw_info.get("entries") or [] if isinstance(entry, dict)]
        if len(entries) <= 1:
            return []
        return entries

    def _coerce_webpage_url(self, info: dict[str, Any], fallback_url: str) -> str:
        for key in ("webpage_url", "original_url", "url"):
            value = str(info.get(key) or "").strip()
            if value.startswith(("http://", "https://")):
                return value
        return fallback_url

    def _pick_filesize(self, info: dict[str, Any]) -> int | None:
        formats = info.get("formats") or []
        for key in ("filesize", "filesize_approx"):
            value = info.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        for item in formats:
            for key in ("filesize", "filesize_approx"):
                value = item.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
        return None

    def _fetch_thumbnail_bytes(self, url: str) -> bytes:
        if not url:
            return b""
        try:
            with build_urllib_opener_for_app_settings(self._settings()).open(url, timeout=10) as response:
                return response.read()
        except Exception:
            return b""

    def _build_output_stem(self, *, title: str, uploader: str, resolution: str, options: DownloadOptions) -> str:
        safe_title = self._sanitize_filename(title or "未命名视频")
        safe_uploader = self._sanitize_filename(uploader or "未知作者")

        if options.naming_rule == NAMING_RULE_TITLE_UPLOADER:
            stem = f"{safe_title} - {safe_uploader}"
        elif options.naming_rule == NAMING_RULE_CUSTOM:
            template = options.custom_template.strip() or "{title}"
            try:
                stem = template.format(title=safe_title, uploader=safe_uploader, author=safe_uploader)
            except Exception as exc:  # noqa: BLE001
                raise VideoDownloadError(f"自定义命名模板无效：{exc}") from exc
        elif options.naming_rule == NAMING_RULE_TITLE:
            stem = safe_title
        else:
            stem = safe_title

        safe_resolution = self._sanitize_filename(resolution or "")
        if safe_resolution:
            suffix = f"[{safe_resolution}]"
            if not stem.endswith(suffix):
                stem = f"{stem} {suffix}".strip()

        stem = self._sanitize_filename(stem.strip())
        return stem or "video"

    def _sanitize_filename(self, value: str) -> str:
        cleaned = WINDOWS_INVALID_FILENAME_PATTERN.sub("_", value)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned[:180]

    def _build_hook(
        self,
        task: DownloadTask,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> Callable[[dict[str, Any]], None]:
        def hook(status: dict[str, Any]) -> None:
            if task.cancel_requested:
                raise DownloadCancelledError("下载已取消。")

            payload = {
                "status": str(status.get("status") or ""),
                "downloaded_bytes": int(status.get("downloaded_bytes") or 0),
                "total_bytes": int(status.get("total_bytes") or 0),
                "total_bytes_estimate": int(status.get("total_bytes_estimate") or 0),
                "speed": float(status.get("speed") or 0),
                "eta": status.get("eta"),
                "fragment_index": int(status.get("fragment_index") or 0),
                "fragment_count": int(status.get("fragment_count") or 0),
                "filename": str(status.get("filename") or ""),
            }
            progress_callback(payload)

        return hook

    def _emit_cli_progress(
        self,
        payload_text: str,
        *,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        parts = payload_text.split("|")
        status, downloaded, total, estimated, speed, eta, fragment_index, fragment_count = (parts + [""] * 8)[:8]
        payload = {
            "status": status,
            "downloaded_bytes": self._parse_int(downloaded),
            "total_bytes": self._parse_int(total),
            "total_bytes_estimate": self._parse_int(estimated),
            "speed": self._parse_float(speed),
            "eta": self._parse_int(eta),
            "fragment_index": self._parse_int(fragment_index),
            "fragment_count": self._parse_int(fragment_count),
            "filename": "",
        }
        progress_callback(payload)

    def _is_cli_merge_line(self, line: str) -> bool:
        lower = line.lower()
        return "[merger]" in lower and "merg" in lower

    def _emit_merge_progress(self, *, progress_callback: Callable[[dict[str, Any]], None]) -> None:
        progress_callback(
            {
                "status": "merging",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "total_bytes_estimate": 0,
                "speed": 0.0,
                "eta": None,
                "fragment_index": 0,
                "fragment_count": 0,
                "filename": "",
            }
        )

    def _parse_int(self, value: str | None) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _parse_float(self, value: str | None) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _resolve_output_file(
        self,
        save_dir: Path,
        output_stem: str,
        info: dict[str, Any],
        task: DownloadTask,
        options: DownloadOptions,
    ) -> Path | None:
        requested_downloads = info.get("requested_downloads") or []
        for item in requested_downloads:
            filepath = item.get("filepath")
            if filepath:
                return Path(filepath)

        ext = "mp4" if options.merge_video_audio or (task.selected_format and task.selected_format.requires_merge) else ""
        if not ext:
            ext = str(info.get("ext") or (task.selected_format.ext if task.selected_format else "") or "mp4")
        return save_dir / f"{output_stem}.{ext}"

    def _snapshot_output_candidates(self, save_dir: Path, output_stem: str) -> dict[Path, tuple[int, int]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in self._iter_output_candidates(save_dir, output_stem):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _cleanup_cancelled_outputs(
        self,
        save_dir: Path,
        output_stem: str,
        preexisting_outputs: dict[Path, tuple[int, int]],
    ) -> None:
        for path in self._iter_output_candidates(save_dir, output_stem):
            try:
                stat = path.stat()
            except OSError:
                continue
            previous = preexisting_outputs.get(path)
            current = (stat.st_size, stat.st_mtime_ns)
            if previous == current:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _iter_output_candidates(self, save_dir: Path, output_stem: str):
        try:
            for path in save_dir.iterdir():
                if path.is_file() and self._is_output_candidate(path, output_stem):
                    yield path
        except OSError:
            return

    def _is_output_candidate(self, path: Path, output_stem: str) -> bool:
        name = path.name
        if not name.startswith(output_stem):
            return False
        suffix = name[len(output_stem) :]
        return not suffix or suffix.startswith((".", "-"))

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def _start_part_progress_watcher(
        self,
        save_dir: Path,
        output_stem: str,
        done_event: threading.Event,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> threading.Thread:
        """轮询 ``.part`` 文件大小，替外部下载器补出进度回调。

        aria2c 这类外部下载器只在整段结束时回调一次 progress_hook，进度条会从 0
        直接跳到 100。这里按固定间隔上报当前 ``.part`` 的字节数，上层
        ``_update_task_download_phase`` 用 ``filename`` 区分视频/音频两个阶段。
        """

        def watch() -> None:
            while not done_event.wait(ARIA2C_PROGRESS_POLL_SECONDS):
                newest = self._newest_part_file(save_dir, output_stem)
                if newest is None:
                    continue
                path, _size = newest
                progress = self._read_aria2_control(path)
                if progress is None:
                    # 控制文件还没写出来（或格式不认识）——宁可不报，也不能拿
                    # 文件大小冒充进度，那是 aria2 写到过的最大偏移，不是下载量。
                    continue
                downloaded, total = progress
                progress_callback(
                    {
                        "status": "downloading",
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "total_bytes_estimate": 0,
                        "speed": 0.0,
                        "eta": None,
                        "fragment_index": 0,
                        "fragment_count": 0,
                        "filename": str(path)[: -len(PART_SUFFIX)],
                    }
                )

        thread = threading.Thread(target=watch, name="krok-download-part-progress", daemon=True)
        thread.start()
        return thread

    def _clear_partial_downloads(self, save_dir: Path, output_stem: str) -> None:
        """删掉 aria2c 留下的半成品，让降级重试从零开始。

        aria2c 是多分片并发写的，``.part`` 的文件大小等于「写到过的最大偏移」，
        不是「已连续下好的字节数」——yt-dlp 内置下载器会拿这个大小当断点续传的
        起点发 ``Range: bytes=<size>-``，分片一旦触到过文件末尾就直接 HTTP 416。
        同名的 ``.aria2`` 控制文件也一并清掉，否则 aria2c 下次会拿它续传。
        """
        for path in self._iter_output_candidates(save_dir, output_stem):
            if not path.name.endswith((PART_SUFFIX, ARIA2C_CONTROL_SUFFIX)):
                continue
            try:
                path.unlink()
            except OSError:
                pass

    # ── 完整性校验 ─────────────────────────────────────────────────────────
    def _verify_downloaded_media(self, task: DownloadTask) -> None:
        """核对产出文件的时长；明显短于预期就判失败并删掉坏文件。

        只在能确定「预期时长」且 ffprobe 可用时才拦截。探测不了就放行——校验的
        职责是挡住已经证实损坏的文件，不是给正常下载添堵。
        """
        path = task.local_file
        expected = float(task.info.duration) if task.info and task.info.duration else 0.0
        if path is None or not path.is_file() or expected <= 0:
            return

        actual = self._probe_shortest_stream_duration(path)
        if actual is None:
            return

        tolerance = max(MIN_DURATION_TOLERANCE_SECONDS, expected * DURATION_TOLERANCE_RATIO)
        if actual >= expected - tolerance:
            return

        try:
            path.unlink()
        except OSError:
            pass
        task.local_file = None
        raise VideoDownloadError(
            f"下载的文件不完整：实际时长仅 {actual:.0f} 秒，应为约 {expected:.0f} 秒。"
            "损坏文件已删除，请重试；如果反复出现，可在下载设置里换一个清晰度。"
        )

    def _probe_shortest_stream_duration(self, path: Path) -> float | None:
        """返回文件里最短的一条流的时长；探测失败返回 None。

        取**最短流**而不是容器时长：静默损坏的典型形态是视频轨被截断、音轨完整，
        此时 ``format.duration`` 跟着音轨走，看起来完全正常。
        """
        try:
            ffprobe = find_tool("ffprobe", Path(str(getattr(self._settings(), "ffmpeg_dir", "") or ".")).expanduser())
        except Exception:  # noqa: BLE001 - 没有 ffprobe 就不做这项校验
            return None

        try:
            completed = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-show_entries", "stream=codec_type,duration",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                timeout=FFPROBE_TIMEOUT_SECONDS,
                env=subprocess_env_for_app_settings(self._settings()),
                creationflags=self._subprocess_creationflags(),
            )
        except Exception:  # noqa: BLE001
            return None
        if completed.returncode != 0:
            return None

        try:
            streams = (json.loads(completed.stdout or "{}") or {}).get("streams") or []
        except (json.JSONDecodeError, ValueError):
            return None

        durations: list[float] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            # 只看音视频轨；封面图会被当成一条没有时长的 video 流，自然被下面过滤掉
            if stream.get("codec_type") not in ("video", "audio"):
                continue
            try:
                duration = float(stream.get("duration"))
            except (TypeError, ValueError):
                continue
            if duration > 0:
                durations.append(duration)
        return min(durations) if durations else None

    def _read_aria2_control(self, part_path: Path) -> tuple[int, int] | None:
        """从 aria2 的 ``.aria2`` 控制文件读出 (已下载字节, 总字节)。

        为什么不能直接用 ``.part`` 的文件大小：aria2 把文件切成 16 段并发下，第 16
        段一开工就写到 15/16 处，文件长度**一秒内**就涨到总大小的 ~94%。拿它当进度
        会让进度条一开始就停在 80~90%（也正是 HTTP 416 那个 bug 的同一个成因）。

        控制文件（version 1）布局：
            version(2) extension(4) infohash_len(4) infohash(N)
            piece_len(4) total_len(8) upload_len(8) bitfield_len(4) bitfield(N)
        bitfield 里每个置位的 bit 代表一个已完成的分片。正在下载中的分片不计入，
        所以这个读数偏保守——但它是单调的，也不会骗人。
        """
        try:
            data = (part_path.parent / f"{part_path.name}{ARIA2C_CONTROL_SUFFIX}").read_bytes()
        except OSError:
            return None

        try:
            offset = 0
            version = int.from_bytes(data[offset:offset + 2], "big")
            if version != 1:
                return None
            offset += 2 + 4  # version + extension
            infohash_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4 + infohash_len
            piece_length = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            total_length = int.from_bytes(data[offset:offset + 8], "big")
            offset += 8 + 8  # total + upload
            bitfield_length = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            bitfield = data[offset:offset + bitfield_length]
        except (IndexError, ValueError):
            return None

        if len(bitfield) != bitfield_length or piece_length <= 0 or total_length <= 0:
            return None
        completed_pieces = sum(bin(byte).count("1") for byte in bitfield)
        return min(completed_pieces * piece_length, total_length), total_length

    def _newest_part_file(self, save_dir: Path, output_stem: str) -> tuple[Path, int] | None:
        newest: tuple[Path, int] | None = None
        newest_mtime = -1.0
        for path in self._iter_output_candidates(save_dir, output_stem):
            if not path.name.endswith(PART_SUFFIX):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime_ns / 1e9 > newest_mtime:
                newest_mtime = stat.st_mtime_ns / 1e9
                newest = (path, stat.st_size)
        return newest

    def _snapshot_child_pids(self) -> set[int]:
        try:
            import psutil

            return {child.pid for child in psutil.Process().children(recursive=True)}
        except Exception:
            return set()

    def _start_cancel_watcher(
        self,
        task: DownloadTask,
        done_event: threading.Event,
        before_pids: set[int],
        *,
        process: subprocess.Popen[str] | None = None,
    ) -> threading.Thread:
        def watch() -> None:
            while not done_event.wait(0.1):
                if not task.cancel_requested:
                    continue
                if process is not None and process.poll() is None:
                    process.terminate()
                self._terminate_new_media_children(before_pids)
                return

        thread = threading.Thread(target=watch, name="krok-download-cancel-watcher", daemon=True)
        thread.start()
        return thread

    def _terminate_new_media_children(self, before_pids: set[int]) -> None:
        try:
            import psutil
        except Exception:
            return

        targets = []
        try:
            children = psutil.Process().children(recursive=True)
        except Exception:
            return

        for child in children:
            if child.pid in before_pids:
                continue
            try:
                name = child.name().lower()
            except Exception:
                name = ""
            if "ffmpeg" in name or "yt-dlp" in name or "aria2c" in name:
                targets.append(child)

        for child in targets:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            _, alive = psutil.wait_procs(targets, timeout=1)
        except Exception:
            alive = targets
        for child in alive:
            try:
                child.kill()
            except Exception:
                pass

    def _subprocess_creationflags(self) -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _build_python_extractor_args(self, extractor_args_hint: str) -> dict[str, dict[str, list[str]]]:
        if self._strip_hint_flags(extractor_args_hint) == YOUTUBE_FALLBACK_EXTRACTOR_ARGS:
            return {"youtube": {"player_client": ["android_vr", "web"]}}
        return {}

    def _strip_hint_flags(self, extractor_args_hint: str) -> str:
        return YOUTUBE_HINT_SEPARATOR.join(
            part
            for part in extractor_args_hint.split(YOUTUBE_HINT_SEPARATOR)
            if part and part != YOUTUBE_DISABLE_COOKIE_HINT
        )

    def _with_no_cookie_hint(self, extractor_args_hint: str) -> str:
        stripped = self._strip_hint_flags(extractor_args_hint)
        return YOUTUBE_HINT_SEPARATOR.join(part for part in (stripped, YOUTUBE_DISABLE_COOKIE_HINT) if part)

    def _hint_disables_cookies(self, extractor_args_hint: str) -> bool:
        return YOUTUBE_DISABLE_COOKIE_HINT in extractor_args_hint.split(YOUTUBE_HINT_SEPARATOR)

    def _should_retry_youtube_with_fallback(
        self,
        url: str,
        message: str,
        extractor_args_hint: str = "",
    ) -> bool:
        if self._strip_hint_flags(extractor_args_hint) == YOUTUBE_FALLBACK_EXTRACTOR_ARGS:
            return False
        if self.detect_source(url) != SOURCE_YOUTUBE:
            return False
        lower = message.lower()
        return (
            "not a bot" in lower
            or "cookies-from-browser" in lower
            or "requested format is not available" in lower
            or "video is not available" in lower
            or "video is unavailable" in lower
            or "downloaded file is empty" in lower
            or "empty file" in lower
            or "空文件" in message
            or "机器人校验" in message
        )

    def _should_retry_youtube_without_cookies(self, url: str, message: str) -> bool:
        if self.detect_source(url) != SOURCE_YOUTUBE:
            return False
        lower = message.lower()
        return (
            "requested format is not available" in lower
            or "当前清晰度不可用" in message
            or "video is not available" in lower
            or "video is unavailable" in lower
        )

    def _normalize_error_message(self, exc: Exception) -> str:
        # yt-dlp 会给 "ERROR:" 加 ANSI 颜色码，直接塞进 Qt label 会显示成
        # ``[0;31mERROR:[0m`` 这种乱码。
        message = ANSI_ESCAPE_PATTERN.sub("", str(exc)).strip() or exc.__class__.__name__
        lower = message.lower()
        if "aria2c" in lower and "exited with code" in lower:
            return (
                "多线程下载器 aria2c 启动失败。已自动改用内置分块下载重试；"
                "如果仍然失败，可以在下载设置里关掉「B 站使用多线程下载（aria2c）」。"
            )
        if "ffmpeg" in lower and ("not found" in lower or "not installed" in lower):
            return "未找到 ffmpeg，无法合并音视频或处理封面。请先安装 ffmpeg 并加入 PATH。"
        if "requested format is not available" in lower:
            return "当前清晰度不可用，请重新解析后选择其他格式。"
        if "downloaded file is empty" in lower or "empty file" in lower:
            return (
                "YouTube 返回了空文件，通常是当前清晰度/播放客户端不可用、Cookie 失效或 yt-dlp 版本偏旧。"
                "已尝试兼容模式；如果仍失败，请刷新 Firefox Cookie、更新 yt-dlp，或换一个清晰度重试。"
            )
        if "not a bot" in lower:
            return "YouTube 触发了机器人校验，已尝试兼容模式；如果仍失败，请稍后重试。"
        if "sign in to confirm your age" in lower or "login required" in lower:
            return "该视频需要登录后访问，请检查 Bilibili 登录状态是否有效。"
        if "http error 403" in lower:
            return "访问被拒绝，可能需要刷新登录状态或稍后重试。"
        if "timed out" in lower:
            return "网络超时，请稍后重试。"
        if "module named yt_dlp" in lower:
            return "本机没有安装 yt_dlp Python 包，也没有可用的 yt-dlp 命令。"
        return message
