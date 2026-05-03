from __future__ import annotations

from dataclasses import replace
from typing import Any

from .download_task import FormatOption


def format_bytes(size: int | None) -> str:
    if size is None or size <= 0:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return "-"


class FormatParser:
    def parse_formats(self, raw_formats: list[dict[str, Any]] | None) -> list[FormatOption]:
        formats = list(raw_formats or [])
        if not formats:
            return []

        best_audio = self._pick_best_audio(formats)
        candidates: dict[str, tuple[float, FormatOption]] = {}

        for item in formats:
            vcodec = str(item.get("vcodec") or "none")
            if vcodec == "none":
                continue

            height = int(item.get("height") or 0)
            width = int(item.get("width") or 0)
            if height <= 0 and width <= 0:
                continue

            resolution = self._build_resolution_label(width, height)
            acodec = str(item.get("acodec") or "none")
            ext = str(item.get("ext") or "").lower()
            format_id = str(item.get("format_id") or "")
            if not format_id:
                continue

            filesize = self._coalesce_size(item)
            requires_merge = acodec == "none" and best_audio is not None
            if requires_merge:
                audio_id = str(best_audio.get("format_id") or "")
                if not audio_id:
                    continue
                download_format = f"{format_id}+{audio_id}"
                filesize = (filesize or 0) + (self._coalesce_size(best_audio) or 0) or None
                audio_codec = str(best_audio.get("acodec") or "unknown")
                format_label = f"{ext.upper() or '视频'} + 音频"
            else:
                download_format = format_id
                audio_codec = acodec
                format_label = ext.upper() or "默认"

            option = FormatOption(
                option_id=f"{format_id}:{resolution}",
                download_format=download_format,
                format_label=format_label,
                resolution=resolution,
                video_codec=self._normalize_codec(vcodec),
                audio_codec=self._normalize_codec(audio_codec),
                filesize=filesize,
                ext=ext,
                note=str(item.get("format_note") or ""),
                height=height,
                width=width,
                requires_merge=requires_merge,
            )

            score = float(item.get("tbr") or item.get("vbr") or item.get("fps") or 0)
            current = candidates.get(resolution)
            if current is None or score >= current[0]:
                candidates[resolution] = (score, option)

        ordered = sorted(
            (item[1] for item in candidates.values()),
            key=lambda option: (option.height, option.width, option.filesize or 0),
            reverse=True,
        )
        if not ordered:
            return []

        recommended = self.build_recommended_option(ordered)
        result = [recommended]
        for option in ordered:
            if option.resolution == recommended.resolution and option.download_format == recommended.download_format:
                continue
            result.append(option)
        return result

    def build_recommended_option(self, options: list[FormatOption]) -> FormatOption:
        if not options:
            raise ValueError("formats cannot be empty")

        best = options[0]
        return replace(
            best,
            option_id=f"recommended:{best.option_id}",
            format_label="最佳质量",
            is_recommended=True,
        )

    def _pick_best_audio(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        audio_only = [
            item
            for item in formats
            if str(item.get("vcodec") or "none") == "none" and str(item.get("acodec") or "none") != "none"
        ]
        if not audio_only:
            return None
        return max(
            audio_only,
            key=lambda item: (
                float(item.get("abr") or 0),
                float(item.get("tbr") or 0),
                self._coalesce_size(item) or 0,
            ),
        )

    def _build_resolution_label(self, width: int, height: int) -> str:
        if height > 0:
            return f"{height}p"
        if width > 0:
            return f"{width}px"
        return "未知"

    def _normalize_codec(self, codec: str) -> str:
        if not codec or codec == "none":
            return "-"
        return codec.split(".")[0]

    def _coalesce_size(self, item: dict[str, Any]) -> int | None:
        for key in ("filesize", "filesize_approx"):
            value = item.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return None
