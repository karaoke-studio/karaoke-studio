"""FFmpeg PCM preparation and safe PyMSS ZIP result extraction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, probe_media, terminate_process


def build_pcm_command(ffmpeg_path: str, input_path: Path, output_path: Path, sample_rate: int) -> list[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        str(output_path),
    ]


def prepare_pcm(
    input_path: str | os.PathLike,
    work_dir: str | os.PathLike,
    *,
    sample_rate: int = 44100,
    ffmpeg_dir: str | os.PathLike | None = None,
    max_seconds: float = 600.0,
    cancelled=None,
) -> tuple[Path, float]:
    source = Path(input_path)
    if not source.is_file():
        raise FileNotFoundError(f"找不到输入音频：{source}")
    configured = Path(ffmpeg_dir) if ffmpeg_dir else None
    ffprobe = find_tool("ffprobe.exe", configured)
    info = probe_media(ffprobe, source)
    if info.audio_streams < 1:
        raise ValueError("所选文件不包含音轨。")
    if info.duration <= 0:
        raise ValueError("无法确定输入音频时长。")
    if max_seconds > 0 and info.duration > max_seconds:
        raise ValueError(f"输入音频超过当前 {max_seconds / 60:g} 分钟限制。")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    required_bytes = int(info.duration * sample_rate * 2 * 4 * 1.1) + 64 * 1024**2
    if shutil.disk_usage(work).free < required_bytes:
        raise OSError("临时目录空间不足，无法准备未压缩音频。")
    output = work / "input.f32le"
    partial = work / "input.f32le.part"
    ffmpeg = find_tool("ffmpeg.exe", configured)
    process = subprocess.Popen(
        build_pcm_command(ffmpeg, source, partial, sample_rate),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **_build_subprocess_kwargs(),
    )
    try:
        while process.poll() is None:
            if cancelled is not None and cancelled.is_set():
                terminate_process(process, timeout=1.0)
                raise InterruptedError("准备音频已取消。")
            time.sleep(0.05)
        stderr = (process.stderr.read() if process.stderr is not None else b"").decode(
            "utf-8", errors="replace"
        )
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg 准备音频失败：{stderr.strip()}")
        partial.replace(output)
        return output, info.duration
    finally:
        partial.unlink(missing_ok=True)


def read_result_manifest(archive: str | os.PathLike) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        try:
            raw = bundle.read("manifest.json")
        except KeyError as exc:
            raise ValueError("PyMSS 结果缺少 manifest.json。") from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("outputs"), list):
        raise ValueError("PyMSS 结果清单格式无效。")
    return payload


def extract_result_stems(
    archive: str | os.PathLike,
    destination: str | os.PathLike,
    *,
    labels: dict[str, str] | None = None,
    base_name: str = "结果",
) -> dict[str, Path]:
    target_root = Path(destination)
    target_root.mkdir(parents=True, exist_ok=True)
    manifest = read_result_manifest(archive)
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as bundle:
        for output in manifest["outputs"]:
            if not isinstance(output, dict):
                continue
            stem = str(output.get("stem", "")).strip()
            filename = str(output.get("filename", "")).strip()
            rel = PurePosixPath(filename)
            if not stem or not filename or rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 1:
                raise ValueError("PyMSS 结果清单包含不安全文件名。")
            try:
                member = bundle.getinfo(filename)
            except KeyError as exc:
                raise ValueError(f"PyMSS 结果缺少音轨：{filename}") from exc
            suffix = Path(filename).suffix.lower()
            label = (labels or {}).get(stem, stem)
            final = _unique_output_path(target_root, f"{base_name}_{label}{suffix}")
            temporary = final.with_suffix(final.suffix + ".part")
            try:
                with bundle.open(member) as source, temporary.open("wb") as sink:
                    shutil.copyfileobj(source, sink, 1024 * 1024)
                os.replace(temporary, final)
            finally:
                temporary.unlink(missing_ok=True)
            extracted[stem] = final
    return extracted


def _unique_output_path(root: Path, filename: str) -> Path:
    candidate = root / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10000):
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("输出目录中存在过多同名音频文件。")


__all__ = [
    "build_pcm_command",
    "extract_result_stems",
    "prepare_pcm",
    "read_result_manifest",
]
