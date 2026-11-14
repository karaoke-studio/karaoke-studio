"""CPU-only Skia/HarfBuzz metrics connection, independent of rendering sessions."""
from __future__ import annotations

from .backend import NativeRendererError, heartbeat_lease_s
from .skia_process import SkiaSidecarProcess


class SkiaTextMetricsProcess(SkiaSidecarProcess):
    def start(self):
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        ready = super().start()
        if ready.get("backend") != "skia" or ready.get("text_metrics_schema") != 1:
            self.abort(reason="Skia metrics handshake failed")
            raise NativeRendererError("sidecar 未声明 Skia 批量度量协议")
        return ready

    def measure(self, fonts: dict, runs: list[dict]) -> list[dict]:
        self._send({"cmd": "measure_text", "metrics_schema": 1, "fonts": fonts, "runs": runs})
        out = self._expect_ok(self._read_until_event("text_measured", timeout_s=self.configure_timeout_s,
                                                    heartbeat_lease_s=heartbeat_lease_s()))
        if out.get("gpu_initialized") is not False or out.get("execution_target") != "cpu_metrics":
            raise NativeRendererError("Skia 规范度量意外创建了 GPU session")
        return out["runs"]
