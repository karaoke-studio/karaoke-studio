"""CPU-only Skia/HarfBuzz metrics connection, independent of rendering sessions."""
from __future__ import annotations

import logging
import time

from .backend import NativeRendererError, heartbeat_lease_s
from .skia_process import SKIA_METRICS_TIMEOUT_S, SKIA_PROTOCOL, SkiaSidecarProcess

_log = logging.getLogger(__name__)


class SkiaTextMetricsProcess(SkiaSidecarProcess):
    def __init__(self, executable_path=None, **kwargs):
        kwargs.setdefault("configure_timeout_s", SKIA_METRICS_TIMEOUT_S)
        super().__init__(executable_path, **kwargs)

    def measure(self, fonts: dict, runs: list[dict]) -> list[dict]:
        started_at = time.monotonic()
        try:
            self._send({"cmd": "measure_text", "skia_protocol": SKIA_PROTOCOL, "fonts": fonts, "runs": runs})
            out = self._expect_ok(self._read_until_event("text_measured", timeout_s=self.configure_timeout_s,
                                                        heartbeat_lease_s=heartbeat_lease_s()))
        except Exception as exc:
            _log.error(
                "Skia 字体度量失败: pid=%s fonts=%d runs=%d elapsed=%.0fms timeout=%.1fs executable=%s err_type=%s err=%s",
                self.process_id,
                len(fonts),
                len(runs),
                (time.monotonic() - started_at) * 1000.0,
                self.configure_timeout_s,
                self.executable_path,
                type(exc).__name__,
                exc,
            )
            raise
        if out.get("gpu_initialized") is not False or out.get("execution_target") != "cpu_metrics":
            raise NativeRendererError("Skia 规范度量意外创建了 GPU session")
        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        if elapsed_ms >= 2000.0:
            _log.warning(
                "Skia 字体度量耗时偏长: pid=%s fonts=%d runs=%d elapsed=%.0fms",
                self.process_id,
                len(fonts),
                len(runs),
                elapsed_ms,
            )
        return out["runs"]
