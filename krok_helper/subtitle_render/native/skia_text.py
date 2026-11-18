"""CPU-only Skia/HarfBuzz metrics connection, independent of rendering sessions."""
from __future__ import annotations

from .backend import NativeRendererError, heartbeat_lease_s
from .skia_process import SkiaSidecarProcess

# Authored-cell glyph policy (existing GPU rule): OpenType shaping only for a
# supplementary scalar, or a multi-scalar cell holding a mark or ZWJ; nominal
# glyphs otherwise, and cell design metrics always from nominal glyphs.
CELL_GLYPH_SCHEMA = 1
CELL_GLYPH_ERROR = 'Skia sidecar 不支持单元字形策略，请重新构建'


def has_cell_glyph_schema(payload: dict) -> bool:
    value = payload.get('cell_glyph_schema')
    return type(value) is int and value == CELL_GLYPH_SCHEMA


def has_kana_mark_schema(payload: dict) -> bool:
    value = payload.get('kana_mark_schema')
    return type(value) is int and value == 1


class SkiaTextMetricsProcess(SkiaSidecarProcess):
    def start(self):
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        ready = super().start()
        if ready.get("backend") != "skia" or ready.get("text_metrics_schema") != 1:
            self.abort(reason="Skia metrics handshake failed")
            raise NativeRendererError("sidecar 未声明 Skia 批量度量协议")
        if ready.get('font_chain_schema') != 1:
            self.abort(reason='Skia font chain handshake failed')
            raise NativeRendererError('Skia sidecar 不支持独立字体链实例，请重新构建')
        if ready.get('mixed_font_schema') != 1:
            self.abort(reason='Skia mixed font handshake failed')
            raise NativeRendererError('Skia sidecar 不支持单元内混合字体，请重新构建')
        if not has_cell_glyph_schema(ready):
            self.abort(reason='Skia cell glyph policy handshake failed')
            raise NativeRendererError(CELL_GLYPH_ERROR)
        if not has_kana_mark_schema(ready):
            self.abort(reason='Skia kana mark policy handshake failed')
            raise NativeRendererError('Skia sidecar 不支持假名浊音字形策略，请重新构建')
        return ready

    def measure(self, fonts: dict, runs: list[dict]) -> list[dict]:
        self._send({"cmd": "measure_text", "metrics_schema": 1, "cell_glyph_schema": CELL_GLYPH_SCHEMA, "kana_mark_schema": 1,
                    "fonts": fonts, "runs": runs})
        out = self._expect_ok(self._read_until_event("text_measured", timeout_s=self.configure_timeout_s,
                                                    heartbeat_lease_s=heartbeat_lease_s()))
        if out.get("gpu_initialized") is not False or out.get("execution_target") != "cpu_metrics":
            raise NativeRendererError("Skia 规范度量意外创建了 GPU session")
        if not has_cell_glyph_schema(out):
            raise NativeRendererError(CELL_GLYPH_ERROR)
        if not has_kana_mark_schema(out):
            raise NativeRendererError('Skia 度量响应缺少假名浊音字形策略')
        return out["runs"]
