"""真实 Qt 平台（windows）下的字体解析正确性基线 —— 子进程契约测试。

**为什么是子进程**：本仓库 conftest 把 ``QT_QPA_PLATFORM`` 固定为
``offscreen``，而 offscreen 平台在这台机器上枚举不到任何字体族
（``QFontDatabase.families() == []``，``QFontInfo().family() == ""``）——
face 级断言在那条会话里只能全部跳过，等于没有基线。字体解析正确性又恰恰
只能对着真实平台的字体库验证，所以在子进程里用默认平台（windows）跑一份
探针，把结果以 JSON 回传，主测试断言：

1. **决策 ↔ 引擎一致**：``synthetic_italic`` 预测 == ``QFontInfo.styleName``
   是否落在族内真实 face 清单之外（实测 MS Gothic/Yu Gothic/Noto Sans SC/
   Microsoft YaHei 合成 'Oblique' 系，Segoe UI/Meiryo 命中真实斜体 face）。
2. **静态就近匹配**：预测 face 字重 == 引擎实际命中 face 的字重。
3. **可变字体轴实例**：中间档（550/650）渲染确实插值（advance 落在两端之间）。
4. **缺字覆盖与回退**：逐簇覆盖判定不误伤组合符/变体选择符；回退族名必须
   **真的覆盖**该字符，且与 Qt 实际选字（QTextLayout glyph runs）一致。
5. **IR 契约**：``*_font_resolved`` 只在非平凡决策出现；``fallback_family``
   只在缺字槽位出现。

环境差异（字体未安装）按探针返回的 ``skipped`` 列表跳过，不硬编码期望字集。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = r'''
import json
import sys

from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics, QRawFont

from krok_helper.subtitle_render.engine.text import font_fallback as fb
from krok_helper.subtitle_render.engine.text.font_capabilities import (
    clear_capabilities_cache,
    get_capabilities,
)
from krok_helper.subtitle_render.engine.text.font_weight import (
    build_weight_font,
    resolve_font_instance,
)
from krok_helper.subtitle_render.engine.text import metrics as text_metrics

result = {"platform": None, "skipped": [], "italic": [], "snap": [], "axis": [],
          "coverage": [], "ir": {}}

from PyQt6.QtWidgets import QApplication
app = QApplication([])
result["platform"] = app.platformName()
if not QFontDatabase.families():
    print(json.dumps(result))
    sys.exit(0)

ITALIC_MATRIX = ("MS Gothic", "Yu Gothic", "Microsoft YaHei", "Noto Sans SC",
                 "Segoe UI", "Meiryo")
SNAP_MATRIX = ("Segoe UI", "Yu Gothic", "Noto Sans SC")
TEXTS = ["A", "e\u0301", "\u6f22", "\ufe0f", "\u6f22\ufe0f", "\u3042",
         "\ud55c", " "]

for family in ITALIC_MATRIX + SNAP_MATRIX:
    if family not in QFontDatabase.families():
        result["skipped"].append(family)

for family in ITALIC_MATRIX:
    if family not in QFontDatabase.families():
        continue
    clear_capabilities_cache()
    instance = resolve_font_instance(family, 400, italic=True)
    info = QFontInfo(build_weight_font(family, 48, 400, italic=True))
    real_styles = list(QFontDatabase.styles(family))
    result["italic"].append({
        "family": family,
        "predicted_synthetic": bool(instance.synthetic_italic),
        "engine_style": info.styleName(),
        "engine_synthetic": info.styleName() not in real_styles,
        "real_styles": real_styles,
    })

for family in SNAP_MATRIX:
    if family not in QFontDatabase.families():
        continue
    clear_capabilities_cache()
    real = {style: int(QFontDatabase.weight(family, style))
            for style in QFontDatabase.styles(family)
            if not QFontDatabase.italic(family, style)}
    for weight in (350, 500, 600, 650, 800, 900):
        instance = resolve_font_instance(family, weight, italic=False)
        info = QFontInfo(build_weight_font(family, 48, weight))
        result["snap"].append({
            "family": family,
            "weight": weight,
            "predicted_face_weight": int(instance.face_weight),
            "predicted_synthetic_bold": bool(instance.synthetic_bold),
            "predicted_variable": bool(instance.is_variable),
            "engine_style": info.styleName(),
            "engine_style_weight": real.get(info.styleName()),
        })

for family in ("Noto Sans SC", "Noto Sans JP"):
    if family not in QFontDatabase.families():
        continue
    clear_capabilities_cache()
    caps = get_capabilities(family)
    if caps is None or not caps.is_variable:
        result["skipped"].append(f"{family} (not variable)")
        continue
    advances = {}
    for weight in (int(caps.axis_min), 550, 650, int(caps.axis_max)):
        font = build_weight_font(family, 64, weight)
        advances[str(weight)] = QFontMetrics(font).horizontalAdvance("Ag")
    result["axis"].append({
        "family": family,
        "min": float(caps.axis_min),
        "max": float(caps.axis_max),
        "advances": advances,
    })

for family in ("Segoe UI", "Microsoft YaHei", "MS Gothic"):
    if family not in QFontDatabase.families():
        continue
    for text in TEXTS:
        missing = fb.missing_clusters(family, 400, False, text)
        fallback = fb.resolve_fallback_family(family, 400, False, missing[0]) if missing else None
        covered_by_fallback = None
        if fallback:
            raw = QRawFont.fromFont(build_weight_font(fallback, 64, 400))
            covered_by_fallback = fb.cluster_covered(raw, missing[0])
        result["coverage"].append({
            "family": family,
            "text": text,
            "missing": list(missing),
            "fallback": fallback,
            "fallback_covers": covered_by_fallback,
        })

# IR 契约：非平凡决策才下发 *_font_resolved
from krok_helper.subtitle_render.native.protocol import (
    _font_face_slot_overrides,
    apply_resolved_font_faces,
)

def emit(payload):
    clone = dict(payload)
    apply_resolved_font_faces(clone)
    return clone

trivial = emit({"font_family": "Segoe UI", "font_weight": 400})
synthetic = emit({"font_family": "MS Gothic", "font_weight": 700, "italic": True})
variable = emit({"font_family": "Noto Sans SC", "font_weight": 650})
result["ir"] = {
    "trivial_has_resolved": "font_resolved" in trivial,
    "trivial_axis": trivial.get("font_axis"),
    "synthetic": synthetic.get("font_resolved"),
    "synthetic_axis": synthetic.get("font_axis"),
    "variable": variable.get("font_resolved"),
    "variable_axis": variable.get("font_axis"),
}

# 缺字回退经预热后随字符下发
from krok_helper.subtitle_render.domain.models import Style, TimingChar
from krok_helper.subtitle_render.native.protocol import timing_char_to_ir

style = Style(font_family="Segoe UI", font_size_px=48)
fb.clear_font_fallback_cache()
fb.prewarm_fallback("Segoe UI", int(style.font_weight), False, ["\u6f22", "A"])
resolved = fb.lookup_fallback("Segoe UI", int(style.font_weight), False, "\u6f22")
result["ir"]["prewarmed_fallback"] = resolved
result["ir"]["char_ir_covered"] = timing_char_to_ir(TimingChar("A", 0))
result["ir"]["char_ir_missing"] = timing_char_to_ir(
    TimingChar("\u6f22", 0), fallback_family=resolved
)

# 批量回退解析 == 逐簇解析（批量化是性能优化，必须逐簇等价）
_batch_clusters = ["\u6f22", "\u3042", "\u30a2", "A", "\u4e2d"]
fb.clear_font_fallback_cache()
_single = {
    cluster: fb.resolve_fallback_family("Segoe UI", 400, False, cluster)
    for cluster in _batch_clusters
}
fb.clear_font_fallback_cache()
_batch = fb.resolve_fallback_families("Segoe UI", 400, False, _batch_clusters)
result["ir"]["batch_equiv"] = {
    cluster: (_single[cluster], _batch.get(cluster)) for cluster in _batch_clusters
}

# 能力层：逐 face 可变判定 + 族级兜底 + 像素级轴指纹
from krok_helper.subtitle_render.engine.text.font_capabilities import (  # noqa: E402
    _axis_is_effective,
    _axis_render_signature,
    clear_capabilities_cache,
    get_capabilities,
)

result["caps"] = {}
for _family in ("Noto Sans SC", "MS Gothic", "Yu Gothic", "Segoe UI Variable",
                "Bahnschrift"):
    clear_capabilities_cache()
    _cap = get_capabilities(_family)
    result["caps"][_family] = None if _cap is None else {
        "faces": len(_cap.faces),
        "axis_present": bool(_cap.axis_present),
        "axis_effective": bool(_cap.axis_effective),
        "variable_style": _cap.variable_style,
        "has_italic_face": bool(_cap.has_italic_face),
    }

# 像素指纹灵敏度：找一对「advance/bbox 相同、像素不同」的相邻轴档
_blind_pair = None
if "Noto Sans SC" in QFontDatabase.families():
    for _w in range(100, 320):
        _c1, _r1 = _axis_render_signature("Noto Sans SC", "Regular", float(_w), 40)
        _c2, _r2 = _axis_render_signature("Noto Sans SC", "Regular", float(_w + 1), 40)
        if _r1 and _r2 and _c1 == _c2 and _r1 != _r2:
            _blind_pair = _w
            break
# 合成加粗阈值（W≥600 且 matched<600）与像素观测的对照表
from krok_helper.subtitle_render.engine.text.font_capabilities import _raster_probe  # noqa: E402
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance  # noqa: E402
from PyQt6.QtGui import QFont as _QFont, QFontInfo as _QFontInfo  # noqa: E402


def _ink_mass(font) -> int:
    font.setPixelSize(56)
    data = _raster_probe(font) or b""
    return sum(1 for i in range(3, len(data), 4) if data[i] > 32)


_sim_rows = []
for _family in ("MS Gothic", "Yu Gothic", "Meiryo", "Microsoft YaHei", "KaiTi",
                "UD Digi Kyokasho N-B", "Arial", "Segoe UI", "Tahoma"):
    if _family not in QFontDatabase.families():
        continue
    clear_capabilities_cache()
    _cap = get_capabilities(_family)
    if _cap is None or _cap.is_variable:
        continue
    for _w in (500, 550, 600, 650, 700, 800, 900):
        _inst = resolve_font_instance(_family, _w)
        if _inst.face_style is None:
            continue
        _req = _QFont(_family); _req.setWeight(_QFont.Weight(_w))
        _face = _QFont(_family); _face.setStyleName(_inst.face_style)
        _face.setWeight(_QFont.Weight(int(_inst.face_weight)))
        _ink_req, _ink_face = _ink_mass(_req), _ink_mass(_face)
        _observed = _ink_req > _ink_face * 1.03
        _sim_rows.append({
            "family": _family, "weight": _w,
            "predicted": bool(_inst.synthetic_bold), "observed": bool(_observed),
            "face_style": _inst.face_style, "face_weight": int(_inst.face_weight),
            "ink_requested": _ink_req, "ink_face": _ink_face,
        })
# shaping 边界留证：Qt(QTextLayout) 会整形，GPU 的逐码点路径不会
from krok_helper.subtitle_render.engine.text.font_fallback import (  # noqa: E402
    _layout_run_families,
)
from krok_helper.subtitle_render.engine.text.font_fallback import slot_font  # noqa: E402
from PyQt6.QtGui import QRawFont as _QRawFont  # noqa: E402

_shaping_rows = []
for _text in ("fi", "ffl", "Á", "が", "क्ष"):
    _font = slot_font("Arial", 400, False)
    _runs = _layout_run_families(_font, _text)
    _shaped = sum(count for _fam, count in _runs)
    _raw = _QRawFont.fromFont(_font)
    _per_codepoint = len(list(_raw.glyphIndexesForString(_text)))
    _shaping_rows.append({
        "text": _text,
        "shaped_glyphs": _shaped,
        "per_codepoint_glyphs": _per_codepoint,
    })
result["shaping"] = _shaping_rows

result["synthetic_bold_rule"] = _sim_rows

result["axis_fingerprint"] = {
    "blind_pair": _blind_pair,
    "endpoints_effective": (
        _axis_is_effective("Noto Sans SC", 100.0, 900.0, "Regular")
        if "Noto Sans SC" in QFontDatabase.families()
        else None
    ),
    "static_fake_axis_effective": _axis_is_effective("MS Gothic", 300.0, 900.0),
}

print(json.dumps(result))
'''


@pytest.fixture(scope="module")
def probe_result() -> dict:
    if os.name != "nt":
        pytest.skip("Windows-only: real-platform font database required")
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["QT_QPA_PLATFORM"] = "windows"
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=300,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    if completed.returncode != 0 or not last_line.startswith("{"):
        pytest.fail(
            "font resolution probe failed:\n"
            f"rc={completed.returncode}\n{stdout[-2000:]}\n"
            f"{completed.stderr.decode('utf-8', errors='replace')[-2000:]}"
        )
    return json.loads(last_line)


def test_probe_ran_on_real_platform(probe_result: dict) -> None:
    assert probe_result["platform"].lower() != "offscreen"
    if not probe_result["italic"] and not probe_result["snap"]:
        pytest.skip("no probe fonts installed")


def test_italic_synthesis_matches_engine(probe_result: dict) -> None:
    assert probe_result["italic"], "probe collected no italic data"
    for row in probe_result["italic"]:
        assert row["predicted_synthetic"] == row["engine_synthetic"], (
            f"{row['family']}: predicted synthetic_italic="
            f"{row['predicted_synthetic']} but engine picked "
            f"{row['engine_style']!r} (faces={row['real_styles']})"
        )


def test_snap_face_weight_matches_engine(probe_result: dict) -> None:
    assert probe_result["snap"], "probe collected no snap data"
    checked = 0
    for row in probe_result["snap"]:
        if row.get("predicted_variable"):
            # 可变字体走轴实例：QFontInfo 报的是最近命名实例的标签，
            # 不是渲染实例——插值正确性由 axis 用例单独验证。
            continue
        if row["engine_style_weight"] is None:
            # 引擎报的是合成名（如 'Bold' 不在真实清单）：合成档由
            # test_weight_resolver / font_weight 的像素校准断言覆盖。
            continue
        checked += 1
        if row["predicted_synthetic_bold"]:
            assert row["predicted_face_weight"] < 600
            continue
        assert row["predicted_face_weight"] == row["engine_style_weight"], (
            f"{row['family']}@{row['weight']}: predicted face "
            f"{row['predicted_face_weight']} but engine picked "
            f"{row['engine_style']}({row['engine_style_weight']})"
        )
    assert checked > 0


def test_variable_axis_interpolates_between_endpoints(probe_result: dict) -> None:
    assert probe_result["axis"], "no variable font installed for the axis probe"
    for row in probe_result["axis"]:
        advances = row["advances"]
        lo = advances[str(int(row["min"]))]
        hi = advances[str(int(row["max"]))]
        mid_lo = advances["550"]
        mid_hi = advances["650"]
        assert lo < hi, f"{row['family']}: axis endpoints render identically"
        assert lo <= mid_lo <= hi and lo <= mid_hi <= hi
        # 中间档必须是真插值：至少与某一端不同。
        assert mid_lo not in (lo, hi) or mid_hi not in (lo, hi), (
            f"{row['family']}: intermediate axis values snapped to endpoints "
            f"({advances})"
        )


def test_cluster_coverage_and_fallback_are_self_consistent(
    probe_result: dict,
) -> None:
    assert probe_result["coverage"], "no coverage rows"
    rows = probe_result["coverage"]
    by_text = {(row["family"], row["text"]): row for row in rows}

    # 组合符 / 变体选择符 / 空白不触发回退。
    for family in {row["family"] for row in rows}:
        for text in ("A", "e\u0301", "\ufe0f", " "):
            row = by_text[(family, text)]
            assert row["missing"] == [], (
                f"{family}: {text!r} wrongly treated as missing "
                f"{row['missing']}"
            )
    # 缺字回退族名必须真的覆盖该字符。
    for row in rows:
        if row["fallback"] is None:
            continue
        assert row["fallback_covers"] is True, (
            f"{row['family']} + {row['text']!r} -> {row['fallback']} "
            "does not actually cover the cluster"
        )


def test_resolved_instance_ir_contract(probe_result: dict) -> None:
    ir = probe_result["ir"]
    assert ir["trivial_has_resolved"] is False
    assert ir["trivial_axis"] is False
    synthetic = ir["synthetic"]
    assert synthetic is not None
    assert synthetic["sim_bold"] is True
    assert synthetic["sim_italic"] is True
    assert synthetic["weight"] == 400
    variable = ir["variable"]
    assert variable is not None
    assert variable["axis"] == 650.0
    assert ir["variable_axis"] is True


def test_missing_cell_carries_python_fallback(probe_result: dict) -> None:
    ir = probe_result["ir"]
    assert "fallback_family" not in ir["char_ir_covered"]
    fallback = ir["prewarmed_fallback"]
    if fallback is None:
        pytest.skip("Qt resolved no fallback family for the probe glyph")
    assert ir["char_ir_missing"]["fallback_family"] == fallback


def test_batched_fallback_matches_per_cluster(probe_result: dict) -> None:
    """批量回退解析（一次 QTextLayout 覆盖全部缺字簇）逐簇等价。

    批量化把缺字工程的冷预热从「每簇一次塑形」降到一次塑形（稳态 1-9ms/
    槽位）；等价性是硬要求——任何不等等于把某个字的回退字体选错。
    """
    equivalence = probe_result["ir"].get("batch_equiv") or {}
    assert equivalence, "probe collected no batch equivalence rows"
    for cluster, (single, batch) in equivalence.items():
        assert single == batch, (
            f"{cluster!r}: per-cluster={single!r} batched={batch!r}"
        )


def test_capabilities_mark_variable_faces_and_family_level_fallback(
    probe_result: dict,
) -> None:
    """逐 face 可变判定 + Qt style 名带前缀时的族级兜底。"""
    caps = probe_result["caps"]
    noto = caps.get("Noto Sans SC")
    if noto is not None:
        assert noto["axis_present"] is True
        assert noto["axis_effective"] is True
        assert noto["variable_style"] is not None  # 逐 face 命中
    ms_gothic = caps.get("MS Gothic")
    if ms_gothic is not None:
        assert ms_gothic["axis_present"] is False
        assert ms_gothic["axis_effective"] is False
    yugothic = caps.get("Yu Gothic")
    if yugothic is not None:
        assert yugothic["axis_present"] is False
    # Segoe UI Variable 的 Qt style 名带光学尺寸前缀（'Small'/'Text'/'Display'
    # 钉回去解析成 'Regular'）→ 逐 face 校验全失败，必须由族级探测兜底。
    seguivar = caps.get("Segoe UI Variable")
    if seguivar is not None:
        assert seguivar["axis_present"] is True, (
            "族级兜底未生效：真可变字体被判静态（会退化成命名实例吸附）"
        )
        assert seguivar["axis_effective"] is True


def test_axis_fingerprint_detects_internal_stroke_changes(probe_result: dict) -> None:
    """像素指纹必须比 advance/bbox 更灵敏（轴只改内部笔画时不漏判）。"""
    fingerprint = probe_result["axis_fingerprint"]
    assert fingerprint["endpoints_effective"] is True
    assert fingerprint["static_fake_axis_effective"] is False
    assert fingerprint["blind_pair"] is not None, (
        "未找到「advance/bbox 相同但像素不同」的相邻轴档——"
        "该环境下无法证明像素指纹的额外灵敏度"
    )


def test_synthetic_bold_rule_matches_pixel_observation(probe_result: dict) -> None:
    """合成加粗阈值（W≥600 且 matched<600）必须与本机引擎的像素表现一致。

    报告口径（2026-10-11 外部评估）：该阈值是「本机 Qt 行为校准」而非行业
    标准。本用例把它升级为**可复现的观测对照**——逐 (族, 字重) 用像素墨量
    比较「请求字重的渲染」与「钉住 face 的渲染」：观测到变粗 ⇔ 规则预测合成。
    （参考面必须同时钉 styleName 与 face 字重：只钉 styleName 时 Qt 会退回
    Regular 参考，实测会造出 15 处假分歧。）
    """
    rows = probe_result.get("synthetic_bold_rule") or []
    if not rows:
        pytest.skip("no static probe fonts installed")
    mismatches = [
        row for row in rows if row["predicted"] != row["observed"]
    ]
    assert not mismatches, mismatches


def test_shaping_boundary_is_recorded(probe_result: dict) -> None:
    """shaping 边界：GPU 逐码点路径 vs Qt 整形路径的已知差异（留证，不是通过项）。

    实测（2026-10-11，CPU painter vs sidecar 逐像素）：
      拉丁连字对 'fi'/'ffl'/'fl'、拉丁组合符 'Á' —— CPU 与 GPU 墨迹盒
      一致（Qt 的逐格 addText 路径同样不组连字）；
      假名分解浊音 'が'（墨量 1638 vs 2058、盒宽 +26%）、
      印度系 'क्ष'（1242 vs 4419）—— **不一致**：GPU 按码点
      取字形（GetGlyphIndices），Qt 侧整形合成单字形并重排位置。

    本用例只钉「整形路径确实存在差异」这一事实与范围：拉丁对齐、组合序列与
    复杂文字列在边界清单里。修法（后续）＝ 逐格下发整形后的**字形编号＋位置**
    （QRawFont.glyphIndexesForString 不整形，须走 QTextLayout glyph runs）。
    """
    rows = {row["text"]: row for row in probe_result.get("shaping") or []}
    if not rows:
        pytest.skip("no shaping probe rows")
    fi = rows.get("fi")
    if fi is not None:
        # 拉丁连字对：整形与逐码点一致（Qt 未组连字）→ 两侧同口径
        assert fi["shaped_glyphs"] == fi["per_codepoint_glyphs"] == 2
    # 分解浊音 / 印度系簇：两条路径的字形数不同（Qt 整形把簇收敛成更少字形，
    # 逐码点路径按码点取字形）→ 位置与墨迹随之分叉。
    for key, codepoints in (("が", 2), ("क्ष", 3)):
        row = rows.get(key)
        if row is None:
            continue
        assert row["per_codepoint_glyphs"] == codepoints
        assert row["shaped_glyphs"] != row["per_codepoint_glyphs"], (
            f"{key!r}: 本环境下整形与逐码点字形数相同，"
            "边界描述需按实际观测更新"
        )
