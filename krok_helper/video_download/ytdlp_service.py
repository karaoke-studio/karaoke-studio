from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import Any, Callable

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


class VideoDownloadError(RuntimeError):
    """Raised when yt-dlp operations fail."""


class DownloadCancelledError(VideoDownloadError):
    """Raised when the user cancels the current download."""


class YtDlpService:
    def __init__(self, format_parser: FormatParser | None = None) -> None:
        self._format_parser = format_parser or FormatParser()

    def extract_info(self, url: str, cookie_file: str | None = None) -> VideoInfo:
        YoutubeDL = self._import_ytdlp()

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        if cookie_file and Path(cookie_file).is_file():
            ydl_opts["cookiefile"] = cookie_file

        try:
            with YoutubeDL(ydl_opts) as ydl:
                raw_info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc

        info = self._unwrap_info(raw_info)
        formats = self._format_parser.parse_formats(info.get("formats"))
        thumbnail_url = str(info.get("thumbnail") or "")
        return VideoInfo(
            url=url,
            source=self.detect_source(url, info.get("extractor_key")),
            title=str(info.get("title") or "未命名视频"),
            uploader=str(info.get("uploader") or info.get("channel") or info.get("owner") or "-"),
            duration=float(info["duration"]) if info.get("duration") else None,
            thumbnail_url=thumbnail_url,
            thumbnail_bytes=self._fetch_thumbnail_bytes(thumbnail_url),
            webpage_url=str(info.get("webpage_url") or url),
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            filesize=self._pick_filesize(info),
            formats=formats,
            recommended_option_id=formats[0].option_id if formats else "",
            subtitles_available=bool(info.get("subtitles") or info.get("automatic_captions")),
        )

    def download(
        self,
        task: DownloadTask,
        options: DownloadOptions,
        progress_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        YoutubeDL = self._import_ytdlp()

        save_dir = Path(options.save_dir).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)
        title = task.title or (task.info.title if task.info else "未命名视频")
        uploader = task.info.uploader if task.info else ""
        output_stem = self._build_output_stem(title=title, uploader=uploader, options=options)
        outtmpl = str(save_dir / f"{output_stem}.%(ext)s")

        selected_format = task.selected_format.download_format if task.selected_format else "best"
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": selected_format,
            "retries": max(0, int(options.retry_count)),
            "socket_timeout": max(1, int(options.timeout)),
            "progress_hooks": [self._build_hook(task, progress_callback)],
            "writethumbnail": bool(options.download_thumbnail),
            "writesubtitles": bool(options.download_subtitle),
            "writeautomaticsub": bool(options.download_subtitle),
        }
        if options.merge_video_audio:
            ydl_opts["merge_output_format"] = "mp4"
        if options.cookie_file and Path(options.cookie_file).is_file():
            ydl_opts["cookiefile"] = options.cookie_file

        try:
            with YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(task.url, download=True)
                final_info = self._unwrap_info(result)
                task.local_file = self._resolve_output_file(save_dir, output_stem, final_info, task, options)
        except DownloadCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VideoDownloadError(self._normalize_error_message(exc)) from exc

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
        except ModuleNotFoundError as exc:
            raise VideoDownloadError("未安装 yt-dlp，请先执行 `pip install yt-dlp`。") from exc
        return YoutubeDL

    def _unwrap_info(self, raw_info: dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_info, dict) and raw_info.get("entries"):
            entries = [entry for entry in raw_info.get("entries") or [] if isinstance(entry, dict)]
            if entries:
                return entries[0]
        return raw_info

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
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read()
        except Exception:
            return b""

    def _build_output_stem(self, *, title: str, uploader: str, options: DownloadOptions) -> str:
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
                "total_bytes": int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0),
                "speed": float(status.get("speed") or 0),
                "eta": status.get("eta"),
                "filename": str(status.get("filename") or ""),
            }
            progress_callback(payload)

        return hook

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

    def _normalize_error_message(self, exc: Exception) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        lower = message.lower()
        if "ffmpeg" in lower and "not found" in lower:
            return "未找到 ffmpeg，无法合并音视频或处理封面。请先安装 ffmpeg 并加入 PATH。"
        if "requested format is not available" in lower:
            return "当前清晰度不可用，请重新解析后选择其他格式。"
        if "sign in to confirm your age" in lower or "login required" in lower:
            return "该视频需要登录后访问，请检查 Bilibili Cookie 是否有效。"
        if "http error 403" in lower:
            return "访问被拒绝，可能需要刷新 Cookie 或稍后重试。"
        if "timed out" in lower:
            return "网络超时，请稍后重试。"
        return message
