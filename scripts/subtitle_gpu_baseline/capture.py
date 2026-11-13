"""Only native hardware GPU commands produce reference pixels."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid
import zlib

from krok_helper.subtitle_render.native.backend import NativeRendererProcess, SharedFrameRingReader
from .artifacts import (SCHEMA, ResourceStore, canonical_bytes, digest, save_frame, pixel_difference,
                        semantic_summary, source_identity, write_json)
from .model import seek_order, sequence_times


def require_hardware(caps: dict) -> None:
    if (not caps.get("available") or caps.get("backend") != "direct2d"
            or caps.get("hardware") is not True or caps.get("warp") is not False):
        raise RuntimeError(f"基线要求真实 D2D GPU，拒绝软件设备或 fallback：{caps}")


def wait_for_prewarm(renderer, render_first, *, timeout_s: float = 90) -> dict:
    """The production wait flag does not imply publication is already complete.

    Render once to release deferred publication, then inspect actual diagnostics
    instead of assuming one warm-up pass stabilized raw/baked path selection.
    """
    deadline = time.monotonic() + timeout_s
    render_first()
    while True:
        diagnostics = renderer.gpu_diagnostics()
        if (not diagnostics.get("realization_enabled", True)
                or not diagnostics.get("realization_supported", True)
                or diagnostics.get("realization_prewarm_complete") is True):
            return diagnostics
        if time.monotonic() >= deadline:
            raise TimeoutError("GPU realization 预热/发布未在限定时间内完成，不能冻结不稳定参考帧。")
        render_first()
        time.sleep(0.01)


class RecordingRenderer(NativeRendererProcess):
    """One protocol interception point records exactly what production sends.

    Vector tables can be omitted on subsequent configure commands by the
    production transport. Save sent IR AND a self-contained expansion.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_ir = None
        self.vector_table = None
        self.expanded_ir = None

    def _send(self, payload):
        if payload.get("cmd") == "configure":
            ir = json.loads(json.dumps(payload["ir"]))
            self.sent_ir = ir
            if "vector_glyphs" in ir:
                self.vector_table = ir["vector_glyphs"]
            expanded = dict(ir)
            if "vector_glyphs" not in expanded and self.vector_table is not None:
                expanded["vector_glyphs"] = self.vector_table
            self.expanded_ir = expanded
        return super()._send(payload)


def capture_cases(*, root: Path, output: Path, executable: Path, cases,
                  width: int = 1920, height: int = 1080, fps: int = 60,
                  sequence_seconds: float = 0, verify_seek: bool = True) -> dict:
    resources = ResourceStore(output)
    manifest = {"schema": SCHEMA, "status": "running", "reference_backend": "direct2d",
        "gpu_api": "d3d11", "scope": "native subtitle overlay, not full product acceptance",
        "width": width, "height": height, "fps": fps, "dpr": 1,
        "source": source_identity(root, executable), "cases": [],
        "render_settings": {"realization_enabled": True, "wait_realizations": True, "worker_count": 1},
        "comparison_policy": "D2D GPU -> Skia GPU; Skia GPU -> Skia Raster; no QPainter oracle"}
    try:
        with RecordingRenderer(executable, response_timeout_s=90, gpu_configure_timeout_s=180) as renderer:
            caps = renderer.backend_info(force_warp=False)
            require_hardware(caps)
            manifest["device"] = caps
            request_serial = 0
            for number, case in enumerate(cases, 1):
                case_dir = output / "cases" / case.case_id
                case_dir.mkdir(parents=True)
                times = (sequence_times(start_ms=0, duration_ms=round(sequence_seconds * 1000), fps=fps)
                         if sequence_seconds else case.timestamps_ms)
                began = time.perf_counter()
                configured = renderer.configure_gpu(case.track, case.style, width=width, height=height,
                    fps=fps, force_warp=False, extra_tracks=list(case.extra_tracks),
                    worker_count=1, wait_realizations=True)
                configure_ms = (time.perf_counter() - began) * 1000
                write_json(case_dir / "sent-ir.json", renderer.sent_ir)
                portable = resources.portable_ir(renderer.expanded_ir)
                write_json(case_dir / "scene-ir.json", portable)
                write_json(case_dir / "semantics.json", semantic_summary(renderer.expanded_ir))
                record = {"id": case.case_id, "features": case.features, "source": case.source,
                    "configure_ms": configure_ms, "configure_result": configured,
                    "scene_sha256": digest(json.dumps(portable, sort_keys=True, ensure_ascii=False).encode()),
                    "frames": [], "seek_mismatches": [], "scope": case.scope}
                manifest["cases"].append(record)
                key = f"gpu_baseline_{uuid.uuid4().hex}"

                def render(t, index):
                    nonlocal request_serial
                    request_serial += 1
                    event = renderer.render_gpu_frame(t, frame_index=index, shm_key=key,
                        generation=number, request_serial=request_serial,
                        force_warp=False, readback_bands=False)
                    if event.get("event") != "gpu_frame_ready":
                        raise RuntimeError(f"GPU 请求未交付参考帧：{event}")
                    with SharedFrameRingReader.from_event(event) as reader:
                        image = reader.read_qimage(event)
                    return image, event

                # Stabilize resource creation before recording; capture every
                # returned path flag so raw/baked changes cannot be hidden.
                warm_started = time.perf_counter()
                record["prewarm_diagnostics"] = wait_for_prewarm(renderer, lambda: render(times[0], 0))
                record["prewarm_wait_ms"] = (time.perf_counter() - warm_started) * 1000
                for index, t in enumerate(times):
                    render(t, index)
                expected = {}
                for index, t in enumerate(times):
                    image, event = render(t, index)
                    frame = {"t_ms": t, "frame_index": index, "native_event": event,
                             **save_frame(case_dir, f"{index:06d}-{t}", image)}
                    record["frames"].append(frame)
                    expected[t] = (frame["pixel_sha256"], event.get("realization_path_ready"), frame["raw_file"])
                if case.require_visible and not any(f["alpha_sum"] for f in record["frames"]):
                    raise RuntimeError(f"正向样例输出全为空帧：{case.case_id}")
                if verify_seek:
                    for index, t in enumerate((*reversed(tuple(expected)), *seek_order(tuple(expected)))):
                        image, event = render(t, index)
                        actual = digest(canonical_bytes(image))
                        if actual != expected[t][0]:
                            original = zlib.decompress((case_dir / expected[t][2]).read_bytes())
                            evidence = save_frame(case_dir / "seek-differences", f"{index:06d}-{t}", image)
                            record["seek_mismatches"].append({"t_ms": t, "expected": expected[t][0],
                                "actual": actual, "expected_path": expected[t][1],
                                "actual_path": event.get("realization_path_ready"), "evidence": evidence,
                                "difference": pixel_difference(original, canonical_bytes(image), width, height)})
                    record["seek_checks"] = len(expected) * 2
                record["diagnostics"] = renderer.gpu_diagnostics()
                write_json(case_dir / "result.json", record)
                write_json(output / "manifest.json", manifest)
                print(f"[{number}/{len(cases)}] {case.case_id}: {len(times)} frames, "
                      f"seek differences={len(record['seek_mismatches'])}", flush=True)
        manifest["status"] = "failed" if any(c["seek_mismatches"] for c in manifest["cases"]) else "passed"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        raise
    finally:
        manifest["resources"] = resources.records
        write_json(output / "manifest.json", manifest)
    return manifest
