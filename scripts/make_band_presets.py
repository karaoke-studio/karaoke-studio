# -*- coding: utf-8 -*-
"""行背景带预设素材生成器（开发用，不参与打包）。

产出：
  1. 预设素材 -> krok_helper/assets/subtitle_render/band_presets/
     静态预设是 PNG；动图预设（五线谱/星火/水泡）是循环 animated WebP。
  2. 评审用预览图（7 张）-> %TEMP%/lin-k-band-preview/

依赖：Pillow + numpy（仅开发机需要，运行时不用）。
用法：
  python scripts/make_band_presets.py
  $env:BAND_PREVIEW_TAG="v11"; python scripts/make_band_presets.py
换 TAG 只是为了换预览文件名、避开看图端的缓存；素材文件本身名字固定。
"""

import math
import os
import pathlib
import random
import tempfile
from pathlib import Path

import numpy as np
from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageFont,
                 ImageSequence)

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "krok_helper" / "assets" / "subtitle_render" / "band_presets"
ASSET_DIR.mkdir(parents=True, exist_ok=True)
TAG = os.environ.get("BAND_PREVIEW_TAG", "v10")
OUT = Path(tempfile.gettempdir()) / "lin-k-band-preview" / TAG
OUT.mkdir(parents=True, exist_ok=True)


def out_png(stem):
    return str(OUT / ("%s_%s.png" % (stem, TAG)))


H = 240

FONT_REG = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
FONT_BOLD = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"]


def font(size, bold=False):
    for path in (FONT_BOLD if bold else []) + FONT_REG:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def new(w, h=H):
    return Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))


def _stops(stops):
    return (np.array([s[0] for s in stops], float),
            np.array([s[1] for s in stops], float))


def vgrad(w, h, stops):
    ts, cs = _stops(stops)
    ys = np.linspace(0, 1, int(h))
    col = np.zeros((int(h), 4))
    for c in range(4):
        col[:, c] = np.interp(ys, ts, cs[:, c])
    arr = np.repeat(col[:, None, :], int(w), axis=1)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def hgrad(w, h, stops):
    ts, cs = _stops(stops)
    xs = np.linspace(0, 1, int(w))
    col = np.zeros((int(w), 4))
    for c in range(4):
        col[:, c] = np.interp(xs, ts, cs[:, c])
    arr = np.repeat(col[None, :, :], int(h), axis=0)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def radial(size, color, power=2.2):
    n = int(size)
    yy, xx = np.mgrid[0:n, 0:n]
    c = (n - 1) / 2
    r = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / (n / 2)
    a = np.clip(1 - r, 0, 1) ** power * color[3]
    arr = np.zeros((n, n, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = color[0], color[1], color[2]
    arr[..., 3] = a.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def glow(layer, color, radius):
    a = layer.split()[3].filter(ImageFilter.GaussianBlur(radius))
    solid = Image.new("RGBA", layer.size, color[:3] + (0,))
    solid.putalpha(a.point(lambda v: int(v * color[3] / 255)))
    return solid


def round_mask(w, h, radius, inset=0):
    m = Image.new("L", (int(w), int(h)), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [inset, inset, w - 1 - inset, h - 1 - inset], radius=radius, fill=255)
    return m


def draw_tile(tw, fn):
    big = new(tw * 3)
    fn(big, tw)
    return big.crop((tw, 0, tw * 2, H))


def put_wrapped(big, tw, cx, cy, sprite):
    x, y = cx - sprite.width / 2, cy - sprite.height / 2
    for off in (-tw, 0, tw):
        big.alpha_composite(sprite, (int(x + off + tw), int(y)))


def _tile_blit(canvas, x, y, sprite, left, tw):
    """按循环单元平铺颗粒：x-tw / x / x+tw 各画一次，跨界处自动绕回。

    中段（``left`` 起、宽 ``tw``）在运行时是横向重复的；颗粒只画一次的话，
    跨界那颗会在平铺缝被切成两半，缝上出现「半个气泡/半颗星」。同口径的
    静态预设用的是 ``put_wrapped``。
    """
    for offset in (-tw, 0, tw):
        canvas.alpha_composite(
            sprite,
            (int(round(x + offset - sprite.width / 2)),
             int(round(y - sprite.height / 2))),
        )


def tint(img, color):
    r, g, b, a = img.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = ImageChops.multiply(rgb, Image.new("RGB", img.size, color))
    return Image.merge("RGBA", (*rgb.split(), a))


def bevel(img, top=44, bottom=88, height=7):
    w = img.width
    img.alpha_composite(vgrad(w, H, [(0, (255, 255, 255, top)),
                                     (height / H, (255, 255, 255, 0))]))
    img.alpha_composite(vgrad(w, H, [(1 - height / H, (0, 0, 0, 0)),
                                     (1, (0, 0, 0, bottom))]))


def add_glow(layer, radius=7, alpha=110):
    """给一层素材加柔和外发光（镂空预设共用）。"""
    blur = layer.split()[3].filter(ImageFilter.GaussianBlur(radius))
    glow = Image.new("RGBA", layer.size, (255, 255, 255, 0))
    glow.putalpha(blur.point(lambda v: int(v * alpha / 255)))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(glow)
    out.alpha_composite(layer)
    return out


# ------------------------------------------------------- 1. anime-ish staff
INK = (92, 62, 40, 245)
CREAM = (252, 240, 220, 232)
GOLD = (255, 196, 92, 255)
CORAL = (255, 132, 150, 255)


def chunky_stroke(cap, pts, width, color, outline, outline_w):
    d = ImageDraw.Draw(cap)
    d.line(pts, fill=outline, width=outline_w, joint="curve")
    d.line(pts, fill=color, width=width, joint="curve")


def note(cap, x, y, size, color, outline):
    d = ImageDraw.Draw(cap)
    head_w, head_h = size * 0.78, size * 0.56
    d.ellipse([x - 2, y - 2, x + head_w + 2, y + head_h + 2], fill=outline)
    d.ellipse([x, y, x + head_w, y + head_h], fill=color)
    sx = x + head_w * 0.72
    d.line([(sx, y + head_h * 0.4), (sx, y - size * 0.95)], fill=outline,
           width=max(3, int(size * 0.15)))
    d.line([(sx, y + head_h * 0.4), (sx, y - size * 0.95)], fill=color,
           width=max(1, int(size * 0.07)))
    d.line([(sx, y - size * 0.95), (sx + size * 0.42, y - size * 0.62)],
           fill=outline, width=max(3, int(size * 0.15)))
    d.line([(sx, y - size * 0.95), (sx + size * 0.42, y - size * 0.62)],
           fill=color, width=max(1, int(size * 0.07)))


def sparkle(cap, cx, cy, r, color):
    layer = new(cap.width, cap.height)
    d = ImageDraw.Draw(layer)
    pts = []
    for i in range(8):
        ang = -math.pi / 2 + i * math.pi / 4
        rad = r if i % 2 == 0 else r * 0.26
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=color)
    glow = radial(int(r * 6), color[:3] + (110,), 2.3)
    cap.alpha_composite(glow, (int(cx - glow.width / 2), int(cy - glow.height / 2)))
    cap.alpha_composite(layer)


NOTE_COLORS = [(209, 74, 79), (58, 110, 165), (78, 138, 94),
               (211, 131, 36), (150, 92, 150)]

INK = (26, 26, 28, 244)
REPO = str(ROOT)
CLEF_FONTS = [r"C:\Windows\Fonts\seguisym.ttf", r"C:\Windows\Fonts\SEGUISYM.TTF",
              r"C:\Windows\Fonts\YuGothM.ttc"]


def load_note_commands():
    """直接取项目里的音符轮廓（particles._note_commands），不自己另画。"""
    import re
    path = os.path.join(REPO, "krok_helper", "subtitle_render", "engine",
                        "render", "effects", "particles.py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(r"def _note_commands\(\)[^\n]*\n[\s\S]*?\n(?=def )", source)
        if match:
            namespace = {"__builtins__": {}, "list": list, "object": object}
            exec(match.group(0), namespace)          # noqa: S102 - 只含字面量
            return namespace["_note_commands"]()
    except Exception:
        pass
    return None


def flatten_commands(commands, steps=26):
    """把 M/L/C/Q/Z 展平成多边形点列（em 坐标，y 向下）。"""
    points, current, start = [], (0.0, 0.0), (0.0, 0.0)
    for command in commands:
        kind = command[0]
        if kind == "M":
            current = (float(command[1]), float(command[2]))
            start = current
            points.append(current)
        elif kind == "L":
            current = (float(command[1]), float(command[2]))
            points.append(current)
        elif kind == "C":
            p0, p1, p2, p3 = current, (command[1], command[2]), (command[3],
                                                                 command[4]), (command[5], command[6])
            for index in range(1, steps + 1):
                t = index / steps
                mt = 1.0 - t
                points.append((
                    mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1],
                ))
            current = (p3[0], p3[1])
        elif kind == "Q":
            p0, p1, p2 = current, (command[1], command[2]), (command[3], command[4])
            for index in range(1, steps + 1):
                t = index / steps
                mt = 1.0 - t
                points.append((
                    mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
                ))
            current = (p2[0], p2[1])
        elif kind == "Z":
            points.append(start)
    return points


NOTE_HEAD_ANCHOR = (0.41, 0.80)   # 音头中心在 sprite 画布中的相对位置


def note_sprite(em_px, color, commands, supersample=4):
    """按项目轮廓渲染一个音符；返回 (sprite, 音头锚点像素)。"""
    size = int(em_px * supersample)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pts = [((x / 500.0 + 1.0) * 0.5 * size, (y / 500.0 + 1.0) * 0.5 * size)
           for x, y in flatten_commands(commands)]
    draw = ImageDraw.Draw(layer)
    draw.polygon(pts, fill=color + (255,))
    # 项目轮廓里符干左缘只到 -330（顶端 -380），与符尾之间留了个楔口；
    # 用一段与符干等宽、顶端收圆的胶囊把这段补齐，接缝就顺了。
    def px(v):
        return (v / 500.0 + 1.0) * 0.5 * size

    draw.rounded_rectangle([px(30.0), px(-380.0), px(80.0), px(-292.0)],
                           radius=px(25.0) - px(0.0), fill=color + (255,))
    layer = layer.resize((em_px, em_px), Image.LANCZOS)
    return layer, (NOTE_HEAD_ANCHOR[0] * em_px, NOTE_HEAD_ANCHOR[1] * em_px)


# ------------------------------------------------ Bravura（SMuFL, OFL 1.1）
MUSIC_FONT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "krok_helper" / "assets" / "subtitle_render" / "fonts" / "Bravura.otf"
)
_NOTEHEAD = "\uE0A4"      # SMuFL noteheadBlack
_STEM_RATIO = 0.12         # 符干宽 = 0.12 个五线间距
_STEM_SPACES = 3.5         # 符干长 = 3.5 个间距
_BEAM_RATIO = 0.5          # 横梁厚 = 0.5 个间距
_font_cache: dict[int, object] = {}
_ink_cache: dict[int, tuple[int, int]] = {}


def music_font(ss):
    """按五线间距取 Bravura 字体（1 em = 4 个间距）。"""
    em = int(ss * 4)
    font = _font_cache.get(em)
    if font is None:
        font = ImageFont.truetype(str(MUSIC_FONT), em)
        _font_cache[em] = font
    return font


def notehead_ink(ss):
    """实测 noteheadBlack 的墨迹：(中线最右 x, 上沿 y)，相对谱头中心。"""
    cached = _ink_cache.get(ss)
    if cached is not None:
        return cached
    probe = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(probe).text((200, 200), _NOTEHEAD, font=music_font(ss),
                               fill=255, anchor="mm")
    px = probe.load()
    right = max(x for x in range(400) if px[x, 200] > 40) - 200
    top = min(y for y in range(400) for x in range(400) if px[x, y] > 40) - 200
    _ink_cache[ss] = (right, top)
    return right, top


def bravura_note_sprite(ss, color, kind, tilt_deg=0.0):
    """一颗完整的雕版音符（含符干/横梁）精灵，按 tilt_deg 整体倾斜。

    kind: quarter / half / eighth_pair / sixteenth_pair
    """
    pad = int(ss * 2)
    if kind in {"eighth_pair", "sixteenth_pair"}:
        width = int(ss * 5.0)
    else:
        width = int(ss * 2.2)
    height = int(ss * 5.2)
    sprite = new(width + pad, height + pad)
    d = ImageDraw.Draw(sprite)
    ink_right, ink_top = notehead_ink(ss)
    stem_w = max(2, int(round(ss * _STEM_RATIO)))
    stem_right = ink_right
    stem_left = stem_right - stem_w
    stem_len = int(ss * _STEM_SPACES)
    cx = pad + int(ss * 0.6)
    cy = pad + height - int(ss * 1.6)          # 谱头中心
    bottom = cy - int(abs(ink_top) * 0.55)
    head = _NOTEHEAD if kind != "half" else "\uE0A3"   # noteheadHalf
    font = music_font(ss)
    if kind in {"eighth_pair", "sixteenth_pair"}:
        gap = int(ss * 2.3)
        for x in (cx, cx + gap):
            d.rectangle([x + stem_left, cy - stem_len,
                         x + stem_right, bottom], fill=color + (255,))
        beam_h = max(4, int(round(ss * _BEAM_RATIO)))
        beams = 2 if kind == "sixteenth_pair" else 1
        for k in range(beams):
            by = cy - stem_len + k * int(beam_h * 1.6)
            d.rectangle([cx + stem_left, by,
                         cx + gap + stem_right, by + beam_h], fill=color + (255,))
        for x in (cx, cx + gap):
            d.text((x, cy), head, font=font, fill=color + (255,), anchor="mm")
    else:
        # 四分/二分音符：向上符干（二分音符符干同长）
        d.rectangle([cx + stem_left, cy - stem_len,
                     cx + stem_right, bottom], fill=color + (255,))
        d.text((cx, cy), head, font=font, fill=color + (255,), anchor="mm")
    if tilt_deg:
        sprite = sprite.rotate(float(tilt_deg), resample=Image.BICUBIC, expand=True)
    return sprite


def draw_bravura_note(target, head_x, head_y, ss, color, rotation=0.0, beam=False):
    """画一个主音符：谱头 + 符干（第七版口径：符干下端止于谱头中线）。

    返回 (符干右沿 x, 符干顶端 y)，供横梁对齐使用。
    """
    ink_right, ink_top = notehead_ink(ss)
    stem_w = max(2, int(round(ss * _STEM_RATIO)))
    stem_right = ink_right                     # 符干右沿 = 谱头墨迹右沿
    stem_left = stem_right - stem_w
    stem_len = int(ss * _STEM_SPACES)
    bottom = head_y - int(abs(ink_top) * 0.55)  # 收在墨迹内部，直角边不露
    d = ImageDraw.Draw(target)
    if not beam:
        d.rectangle([head_x + stem_left, head_y - stem_len,
                     head_x + stem_right, bottom], fill=color + (255,))
    # 谱头最后画，压住符干下端
    d.text((head_x, head_y), _NOTEHEAD, font=music_font(ss),
           fill=color + (255,), anchor="mm")
    return head_x + stem_right, head_y - stem_len


def draw_bravura_beamed(target, head_x, head_y, ss, color, gap_spaces=2.3, beams=1):
    """连音符：两个主音符 + 符干顶端横梁（beams=2 为十六分音符）。"""
    ink_right, ink_top = notehead_ink(ss)
    stem_w = max(2, int(round(ss * _STEM_RATIO)))
    stem_right = ink_right
    stem_left = stem_right - stem_w
    stem_len = int(ss * _STEM_SPACES)
    bottom = head_y - int(abs(ink_top) * 0.55)
    x2 = head_x + int(ss * gap_spaces)
    d = ImageDraw.Draw(target)
    for x in (head_x, x2):
        d.rectangle([x + stem_left, head_y - stem_len,
                     x + stem_right, bottom], fill=color + (255,))
    for x in (head_x, x2):
        d.text((x, head_y), _NOTEHEAD, font=music_font(ss),
               fill=color + (255,), anchor="mm")
    beam_h = max(4, int(ss * _BEAM_RATIO))
    for k in range(beams):
        by = head_y - stem_len + k * int(beam_h * 1.6)
        d.rectangle([head_x + stem_left, by, x2 + stem_right, by + beam_h],
                    fill=color + (255,))
    return x2


def paste_note(target, head_x, head_y, em_px, color, commands):
    sprite, anchor = note_sprite(em_px, color, commands)
    target.alpha_composite(sprite, (int(head_x - anchor[0]), int(head_y - anchor[1])))


def _paste_rotated_note(target, head_x, head_y, em_px, color, commands, rotation):
    """按谱头位置贴一个旋转过的音符（装饰用，角度随机更自然）。"""
    sprite, anchor = note_sprite(em_px, color, commands)
    if rotation:
        sprite = sprite.rotate(
            float(rotation), resample=Image.BICUBIC, expand=True
        )
        # 旋转后以中心对齐原锚点
        target.alpha_composite(
            sprite,
            (
                int(head_x - sprite.width / 2),
                int(head_y - sprite.height / 2),
            ),
        )
        return
    target.alpha_composite(
        sprite, (int(head_x - anchor[0]), int(head_y - anchor[1]))
    )


def text_sprite(text, height, color, font_paths=None):
    """把一段文本渲染成指定高度的白色精灵（用于升降号等乐理符号）。"""
    for path in (font_paths or CLEF_FONTS):
        if not os.path.exists(path):
            continue
        try:
            font_obj = ImageFont.truetype(path, 400)
        except Exception:
            continue
        canvas = Image.new("RGBA", (1600, 900), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text(
            (60, 200), text, font=font_obj, fill=color + (255,)
        )
        box = canvas.getbbox()
        if not box:
            continue
        glyph = canvas.crop(box)
        scale = height / max(glyph.height, 1)
        return glyph.resize(
            (max(int(glyph.width * scale), 1), height), Image.LANCZOS
        )
    return None

def clef_sprite(height, color, *, outline=None, outline_width=0):
    """真高音谱号：抓 Segoe UI Symbol 的 U+1D11E，裁到墨迹后缩放到目标高度。"""
    for path in CLEF_FONTS:
        if not os.path.exists(path):
            continue
        try:
            font_obj = ImageFont.truetype(path, 400)
        except Exception:
            continue
        canvas = Image.new("RGBA", (900, 900), (0, 0, 0, 0))
        drawer = ImageDraw.Draw(canvas)
        if outline is not None and outline_width > 0:
            drawer.text(
                (200, 200), "\U0001D11E", font=font_obj,
                fill=color + (255,), stroke_width=outline_width,
                stroke_fill=outline + (255,),
            )
        else:
            drawer.text((200, 200), "\U0001D11E", font=font_obj,
                        fill=color + (255,))
        box = canvas.getbbox()
        if not box:
            continue
        glyph = canvas.crop(box)
        scale = height / max(glyph.height, 1)
        return glyph.resize((max(int(glyph.width * scale), 1), height), Image.LANCZOS)
    return None


def build_staff_scroll():
    """五线谱（用户口径 2026-10-24）：

    * 全透明无底板；
    * **谱线层**：5 条白线 + 终止双竖线，带一层柔和外发光（参考图那种柔光）；
    * **谱号层**：白色高音谱号，带柔和外发光；
    * **素材本体**：四个彩色音符（用户的独有设计，保留），运行时压在最上层；
    * **静态变体**：同一张图去掉中段漂浮的小音符（只保留四个静态音符），
      供「中间动态音符」开关关闭时使用；与动图同尺寸、同端点。
    * 成图 1920x240，中段 1570px。
    """
    w, L, R = 1920, 190, 160
    commands = load_note_commands() or []
    if not commands:
        # 音符轮廓缺失时静态层/动图会只剩谱线，绝不能静默覆盖素材。
        raise RuntimeError(
            "staff_scroll: 找不到项目音符轮廓 "
            "(krok_helper/subtitle_render/engine/render/effects/particles.py::"
            "_note_commands())；拒绝生成空音符素材"
        )

    # 谱线层：五线 + 终止双竖线。
    lines = new(w)
    ld = ImageDraw.Draw(lines)
    for y in (64, 92, 120, 148, 176):
        ld.line([(0, y), (w, y)], fill=(255, 255, 255, 255), width=3)
    ld.line([(w - 13, 50), (w - 13, 190)], fill=(255, 255, 255, 255), width=3)
    ld.line([(w - 3, 50), (w - 3, 190)], fill=(255, 255, 255, 255), width=7)
    lines = add_glow(lines)

    # 谱号层：高音谱号（运行时乘谱号色）。
    clef_layer = new(w)
    clef = clef_sprite(140, (255, 255, 255))
    if clef is None:
        # 缺 Segoe UI Symbol（或兼容字体）时 clef_sprite 返回 None；若继续
        # 保存，会把全透明谱号层覆盖进仓库，成片里的谱号会永久消失。
        raise RuntimeError(
            "staff_scroll: 找不到可用的高音谱号字体（需要 Segoe UI Symbol "
            "或 CLEF_FONTS 里的兼容字体）；拒绝生成空白谱号层"
        )
    clef_layer.alpha_composite(clef, (10, 120 - clef.height // 2))
    clef_layer = add_glow(clef_layer)

    # 素材本体（动图）：四个彩色音符 + 中段缓慢漂浮的小音符（用户的独有
    # 设计，压在谱线/谱号之上）。2026-10-24 第二轮用户口径：「音符漂移太快」
    # → 中段音符改成每循环只走一圈很小的椭圆（横向 ±1.2~2.2px、纵向
    # ±0.8~1.4px，匀速、没有来回折返），再加一点极轻的摇曳。
    drift = []
    rnd = random.Random(41)
    tw = w - L - R
    # 每种音符各一个：四分音符 / 八分连音符 / 十六分连音符 / 二分音符（空心）
    kinds = ("quarter", "eighth_pair", "sixteenth_pair", "half")
    for index, kind in enumerate(kinds):
        drift.append((
            L + (index + 0.5) * tw / len(kinds) + rnd.uniform(-50, 50),
            96 + rnd.random() * (H - 176),
            21 + rnd.random() * 4,           # 五线间距 ss（主音符大小，不压歌词）
            NOTE_COLORS[index % len(NOTE_COLORS)],
            rnd.random(),                    # 相位（占一整圈的比例）
            1.2 + rnd.random() * 1.0,        # 横向漂移半径（px，每循环一圈）
            0.8 + rnd.random() * 0.6,        # 纵向漂移半径（px，每循环一圈）
            kind,
            rnd.uniform(-90.0, 90.0),        # 整颗音符随机倒伏角（度，±90）
            rnd.uniform(0.8, 1.6),           # 摇曳幅度（度，每循环一个来回）
        ))
    # 60 帧 / 20ms 不变（不拉长帧时长）：整帧按 2 倍超采样作画再降采样，
    # 漂移量只有半像素级也不会「一帧跳一格」，播放依然顺滑。
    ss_frame = 2
    frames = []
    static = new(w * ss_frame, H * ss_frame)
    if commands:
        cap = new(L * ss_frame, H * ss_frame)
        paste_note(cap, 108 * ss_frame, 176 * ss_frame, 92 * ss_frame,
                   NOTE_COLORS[0], commands)
        paste_note(cap, 146 * ss_frame, 132 * ss_frame, 76 * ss_frame,
                   NOTE_COLORS[1], commands)
        static.alpha_composite(cap, (0, 0))
        cap = new(R * ss_frame, H * ss_frame)
        paste_note(cap, 40 * ss_frame, 176 * ss_frame, 92 * ss_frame,
                   NOTE_COLORS[2], commands)
        paste_note(cap, 82 * ss_frame, 132 * ss_frame, 76 * ss_frame,
                   NOTE_COLORS[3], commands)
        static.alpha_composite(cap, ((w - R) * ss_frame, 0))
    for frame in range(FRAME_COUNT):
        t = frame / max(FRAME_COUNT - 1, 1)
        big = static.copy()
        for x, y, ss, color, phase, radius_x, radius_y, kind, tilt, rock in drift:
            arc = math.tau * (t + phase)
            dx = math.cos(arc) * radius_x
            dy = math.sin(arc) * radius_y
            angle = tilt + math.sin(arc) * rock
            sprite = bravura_note_sprite(ss * ss_frame, color, kind, angle)
            big.alpha_composite(
                sprite,
                (int(round((x + dx) * ss_frame - sprite.width / 2)),
                 int(round((y + dy) * ss_frame - sprite.height / 2))),
            )
        frames.append(big.resize((w, H), Image.LANCZOS))

    # 静态变体：与每帧同源、同缩放，只是没有中段漂浮的小音符。
    static_frame = static.resize((w, H), Image.LANCZOS)
    _require_non_empty_asset(static_frame, "staff_scroll_static")
    if static_frame.crop((L, 0, w - R, H)).getbbox() is not None:
        raise RuntimeError(
            "staff_scroll: 静态层跨过了设计端点（中段应为全透明）；"
            "请检查音符位置/大小，拒绝覆盖素材"
        )
    return frames, L, R, lines, clef_layer, static_frame

# --------------------------------------------------------- 2. minimal stars
def clean_star(r, alpha):
    n = int(r * 6) | 1
    im = new(n, n)
    d = ImageDraw.Draw(im)
    c = n / 2
    pts = []
    for i in range(8):
        ang = -math.pi / 2 + i * math.pi / 4
        rad = r if i % 2 == 0 else r * 0.30
        pts.append((c + rad * math.cos(ang), c + rad * math.sin(ang)))
    d.polygon(pts, fill=(255, 255, 255, alpha))
    return im.filter(ImageFilter.GaussianBlur(0.4))


def _stars_mid(big, tw):
    rnd = random.Random(17)
    count = max(1, int(round(tw * 16 / 320)))
    for _ in range(count):
        r = 2.2 + rnd.random() ** 2 * 3.6
        put_wrapped(big, tw, rnd.random() * tw, 30 + rnd.random() * (H - 60),
                    clean_star(r, 255))


def _star4_points(radius, inner_ratio=0.11, samples=14):
    """与项目内星光粒子同款：四角星 + 四段二次贝塞尔凹边。"""
    outer = 500.0
    inner = outer * inner_ratio
    # (起点, 控制点, 终点) 四段
    segments = [
        ((0.0, -outer), (inner, -inner), (outer, 0.0)),
        ((outer, 0.0), (inner, inner), (0.0, outer)),
        ((0.0, outer), (-inner, inner), (-outer, 0.0)),
        ((-outer, 0.0), (-inner, -inner), (0.0, -outer)),
    ]
    pts = []
    for start, control, end in segments:
        for index in range(samples):
            t = index / samples
            x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t ** 2 * end[0]
            y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t ** 2 * end[1]
            pts.append((x / outer * radius, y / outer * radius))
    return pts


def _star_glow(r, alpha):
    """四角星光：star4 凹边星形（4 倍超采样保住细星臂）+ 背后径向柔光。

    动图是 8 bit alpha 的 WebP，柔光就是真柔光，不需要点阵近似。
    """
    size = int(max(r * 3.4 * 2.6, 15)) | 1
    ss = 4                                   # 超采样倍率
    big = size * ss
    c = big / 2.0
    im = new(big, big)
    # 星臂要明显长于光核，否则会糊成圆点（用户实测"星星变圆球"）
    pts = [(c + x * ss, c + y * ss) for x, y in _star4_points(r * 1.6)]
    ImageDraw.Draw(im).polygon(pts, fill=(255, 255, 255, alpha))
    im = im.resize((size, size), Image.LANCZOS)
    glow = radial(int(max(r * 3.4, 11)) | 1, (255, 255, 255, int(alpha * 0.45)), 2.0)
    out = new(size, size)
    out.alpha_composite(glow, (int(size / 2 - glow.width / 2),
                               int(size / 2 - glow.height / 2)))
    out.alpha_composite(im)
    return out.filter(ImageFilter.GaussianBlur(max(0.15, r * 0.03)))


def build_stars():
    """星火点阵（动图）：按项目星光粒子的生命周期实现。

    每颗星有出生相位与寿命：出生 → 放大到满 → 淡出，错峰循环
    （同 `particles.py` 的 star4 粒子思路），60 帧 / 1.2s 内闪一次。
    """
    w, L, R = 1920, 120, 120
    tw = w - L - R
    rnd = random.Random(17)
    stars = []
    count = max(1, int(round(tw * 22 / 320)))
    for _ in range(count):
        stars.append((
            L + rnd.random() * tw,              # 循环单元内 x（跨界自动绕回）
            26 + rnd.random() * (H - 52),
            2.4 + rnd.random() ** 2 * 4.6,     # 星臂半径
            rnd.random(),                       # 出生相位
            0.45 + rnd.random() * 0.55,         # 寿命（占一个循环的比例）
        ))
    frames = []
    for frame in range(FRAME_COUNT):
        t = frame / max(FRAME_COUNT - 1, 1)
        img = new(w)
        for x, y, r, birth, life in stars:
            local = ((t - birth) % 1.0) / life
            if local >= 1.0:
                continue
            # 出生→放大、消失→淡出：sin 包络两端为 0
            envelope = math.sin(math.pi * local) ** 1.25
            if envelope <= 0.02:
                continue
            scale = 0.55 + 0.45 * local
            _tile_blit(img, x, y,
                       _star_glow(r * scale, int(255 * envelope)), L, tw)
        frames.append(img)
    return frames, L, R


# ------------------------------------- 3. neutral satin ribbon (tintable)
def build_diagonal():
    """斜纹（镂空轻盈版）：只有斜向细条纹，半透明 + 柔光，没有底板/折边。"""
    w, L, R = 1920, 96, 96
    tw = w - L - R                      # 1728; stripe period MUST divide it
    period = 48
    assert tw % period == 0, "middle tile width must be a multiple of the stripe period"

    img = new(w)
    stripes = ImageDraw.Draw(img)
    # 实体条纹：不透明度拉满（用户口径 2026-10-24），只用一条实心斜纹
    # 加一条更亮的细芯，不再靠半透明叠色制造"雾感"。
    for k in range(-14, w // period + 20):
        x0 = k * period
        stripes.polygon([(x0, 0), (x0 + 14, 0), (x0 + 14 - H, H), (x0 - H, H)],
                        fill=(255, 255, 255, 255))
    img = add_glow(img.filter(ImageFilter.GaussianBlur(0.5)), 3, 60)
    return img, L, R



# ------------------------------------------------------ 4. clean blue bubbles
BLUES = [(150, 226, 255), (196, 240, 255), (118, 196, 236), (168, 255, 240)]

# ------------------------------------------------------ 4. clean blue bubbles
BLUES = [(150, 226, 255), (196, 240, 255), (118, 196, 236), (168, 255, 240)]


def _lighten(color, amount):
    return tuple(int(value + (255 - value) * amount) for value in color)


def bubble_sprite(r, color):
    """一颗水泡：淡填充 + 近白亮环 + 左上高光，画法照搬扫字线「气泡柱」。

    `paint_scanline_bubbles` 的口径（用户 2026-10-24「好看的气泡要参考气泡柱的
    画法」）：

    * 填充 alpha 70/255，几乎是透明的；
    * 亮环 alpha 205/255、线宽≈半径的 10%，颜色往白色提 0.55；
    * 高光 alpha 215/255，半径 0.18R，圆心在左上 (−0.32R, −0.32R)，往白色提 0.85。

    轮廓半径 2.7r（加上亮环≈用户验收过的 2.8r）。动图是 8 bit alpha 的 WebP，
    半透明就是**真**半透明。精灵按 1× 出（不做超采样）：动图素材是逐帧位图，
    带抗锯齿的边缘会让 alpha 出现几十级灰度，无损 WebP 体积直接翻十倍；运行
    时带子本来就会按 0.5 倍左右平滑降采样，边缘的抗锯齿由那一步提供。
    """
    radius = 2.7 * r
    ss = 1
    size = int(radius * 2 + 4)
    big = size * ss
    centre = (big - 1) / 2.0
    ring_radius = radius * ss
    box = [centre - ring_radius, centre - ring_radius,
           centre + ring_radius, centre + ring_radius]
    fill_layer = Image.new("RGBA", (big, big), color + (0,))
    ImageDraw.Draw(fill_layer).ellipse(box, fill=color + (70,))
    rim_color = _lighten(color, 0.55)
    ring_layer = Image.new("RGBA", (big, big), rim_color + (0,))
    ImageDraw.Draw(ring_layer).ellipse(
        box,
        outline=rim_color + (205,),
        width=max(int(round(ring_radius * 0.10)), 2),
    )
    core_color = _lighten(color, 0.85)
    hx = centre - ring_radius * 0.32
    hr = max(ring_radius * 0.18, 2.0)
    core_layer = Image.new("RGBA", (big, big), core_color + (0,))
    ImageDraw.Draw(core_layer).ellipse(
        [hx - hr, hx - hr, hx + hr, hx + hr], fill=core_color + (215,)
    )
    sprite = Image.alpha_composite(
        Image.alpha_composite(fill_layer, ring_layer), core_layer
    )
    return sprite.resize((size, size), Image.LANCZOS)


def build_bubbles():
    """水泡（动图）：粒子式缓慢上浮 + 缓慢左右摆动，首尾严格同帧。

    2026-10-24 第二轮用户口径（「上升速度过快、抖动速度过快过密」）：
    * 上浮速度：每循环只上浮 ``H * 0.14``（原来 0.25，且一半颗粒跑两个循环）；
    * 摆动：**每循环只摆一个来回**、幅度 ±1.8~4.4px（原来每循环 2~4 个来回、
      最大 ±18px，峰值横向速度是现在的十几倍）；
    * 密度：颗粒数降到原来的 45%（每 320px 60 颗 → 中段 1600px 约 300 颗）；
      「抖动过密」也一并缓解，不再糊成一片；
    * 颗粒按循环单元取模平铺，跨界那颗在另一侧补画，平铺缝上不会出现半个泡。
    """
    w, L, R = 1920, 160, 160
    tw = w - L - R
    rnd = random.Random(29)
    bubbles = []
    count = max(1, int(round(tw * 60 / 320)))
    for _ in range(count):
        bubbles.append((
            L + rnd.random() * tw,              # 循环单元内的 x
            rnd.random(),                       # 相位（生命进度）
            2.5 + rnd.random() ** 1.8 * 8.5,    # 半径（沿用旧素材尺寸）
            rnd.choice(BLUES),
            rnd.random(),                       # 摆动相位（一圈的比例）
            1.8 + rnd.random() * 2.6,           # 摆动幅度（±px）
            H * (0.05 + rnd.random() * 0.9),    # 出生 y
            H * 0.14,                            # 每循环上浮高度
        ))
    sprites = [bubble_sprite(item[2], item[3]) for item in bubbles]
    fade = 0.12                                  # 出生/消失各占生命的比例
    frames = []
    for frame in range(FRAME_COUNT):
        t = frame / max(FRAME_COUNT - 1, 1)
        img = new(w)
        for item, sprite in zip(bubbles, sprites):
            x, start, _r, _color, sway, swing, lane_y, travel = item
            progress = (start + t) % 1.0
            # 出生/消失各留 12% 的渐变，循环点两侧都收到 alpha 0。
            envelope = min(1.0, progress / fade, (1.0 - progress) / fade)
            if envelope <= 0.0:
                continue
            lut = [int(value * envelope) for value in range(256)]
            visible = sprite.copy()
            visible.putalpha(sprite.getchannel("A").point(lut))
            dx = math.sin(math.tau * (t + sway)) * swing
            _tile_blit(img, x + dx, lane_y - progress * travel, visible, L, tw)
        frames.append(img)
    return frames, L, R


# -------------------------------------- 5. neutral glass pill (tintable)
def build_plain_round():
    """纯色圆角条：像 galgame 对话框那样的圆板。

    用户口径 2026-10-24：这是一块**有厚度的圆角面板**——接近不透明的白色
    底（上浅下深一点点的渐变）+ 收边描边 + 柔和外发光，压在任意背景上都
    像一个对话框，而不是一条半透明玻璃条。
    """
    w, L, R = 1920, 64, 64
    img = new(w)
    body = vgrad(w, H, [(0, (255, 255, 255, 238)),
                        (0.55, (250, 251, 253, 232)),
                        (1, (236, 239, 244, 228))])
    mask = round_mask(w, H, 30)
    img.paste(body, (0, 0), mask)
    # 顶部高光 + 内侧一圈亮边，做出"面板"的厚度。
    sheen = vgrad(w, H, [(0, (255, 255, 255, 120)),
                         (0.18, (255, 255, 255, 0))])
    sheen.putalpha(ImageChops.multiply(sheen.split()[3], mask))
    img.alpha_composite(sheen)
    border = new(w)
    ImageDraw.Draw(border).rounded_rectangle(
        [1, 1, w - 2, H - 2], radius=30,
        outline=(255, 255, 255, 235), width=2,
    )
    img.alpha_composite(border)
    # 外沿一圈很淡的暗边，避免白底在亮背景上糊掉。
    edge = new(w)
    ImageDraw.Draw(edge).rounded_rectangle(
        [0, 0, w - 1, H - 1], radius=30,
        outline=(60, 66, 78, 60), width=1,
    )
    img.alpha_composite(edge)
    return add_glow(img, 5, 80), L, R


# ---------------------------------------------------------------- assembly
PRESETS = []


FRAME_MS = 20          # 动图预设每帧时长（20ms ≈ 50fps）
FRAME_COUNT = 60       # 动图预设帧数（60 × 20ms = 1200ms 一个循环）

ANIM_EXT = ".webp"     # 动图预设用 animated WebP：带 8 bit alpha


def save_animated_asset(frames, path, frame_ms=None):
    """写循环动图素材（animated WebP）：**严格第一帧 = 最后一帧**。

    用 WebP 而不是 GIF，是因为 GIF 只有 1 bit 透明：水泡那层 alpha≈70 的淡填充、
    星火的柔光都只能靠点阵近似，放大看是噪点/网点，整段动图还会被调色板/透明
    下标坑（曾出现个别帧整帧不透明、首尾帧量化不一致）。WebP 是真 8 bit alpha，
    扫字线「气泡柱」那套半透明画法可以原样搬进素材；CPU 预览与 sidecar 都用
    `QImageReader` 解码，两边像素完全一致。
    """
    frames = list(frames)
    if not frames:
        return
    frames[-1] = frames[0].copy()                  # 首尾严格同帧（循环点）
    frames[0].save(
        str(path),
        save_all=True,
        append_images=frames[1:],
        duration=int(frame_ms or FRAME_MS),
        loop=0,
        format="WEBP",
        lossless=True,          # 细亮环/柔光用有损会起毛刺，素材体积本来就不大
        quality=100,
        method=4,
    )


def check_animated_asset(path, total_ms=None, expect_translucent=False):
    """回读校验：首尾同帧、总时长对得上、（可选）确实带半透明像素。"""
    with Image.open(path) as image:
        frames = []
        delays = []
        for frame in ImageSequence.Iterator(image):
            frames.append(frame.convert("RGBA").copy())
            delays.append(int(frame.info.get("duration") or 0))
    if not frames:
        raise AssertionError("%s has no frames" % path)
    first = np.asarray(frames[0])
    same = np.array_equal(first, np.asarray(frames[-1]))
    alpha = first[..., 3]
    translucent = bool(((alpha > 0) & (alpha < 255)).any())
    print("  loop-check %-36s frames=%3d first==last=%-5s alpha=%s total=%dms"
          % (path.name, len(frames), same,
             "8bit" if translucent else "binary", sum(delays)))
    if not same:
        raise AssertionError("%s: first frame != last frame" % path)
    if expect_translucent and not translucent:
        raise AssertionError("%s: no translucent pixels (alpha is binary)" % path)
    if total_ms is not None and sum(delays) != total_ms:
        raise AssertionError("%s: loop length %dms != %dms"
                             % (path, sum(delays), total_ms))


def _require_non_empty_asset(image, name):
    """拒绝把空白层写进仓库（通常是缺字体/音符轮廓导致的静默失败）。"""
    if image.getbbox() is None:
        raise RuntimeError(
            "%s 层是空白的（缺少字体或项目音符轮廓？），拒绝覆盖已提交素材"
            % name
        )


def register(preset_id, label, builder, height_pct, tintable=False, default_tint=None,
             feather_px=0.0, animated=False, frame_ms=None):
    built = builder()
    clef_name = ""
    if animated:
        # 动图预设：builder 返回 (frames, L, R[, lines, clef[, static]])。
        frames = built[0]
        rest = list(built[1:])
        anim_path = ASSET_DIR / (preset_id + ANIM_EXT)
        save_animated_asset(frames, anim_path, frame_ms)
        check_animated_asset(
            anim_path,
            total_ms=len(frames) * int(frame_ms or FRAME_MS),
            expect_translucent=preset_id != "staff_scroll",
        )
        lines_name = ""
        static_name = ""
        if len(rest) >= 4:
            _require_non_empty_asset(rest[2], preset_id + "_lines")
            _require_non_empty_asset(rest[3], preset_id + "_clef")
            rest[2].save(str(ASSET_DIR / (preset_id + "_lines.png")))
            lines_name = preset_id + "_lines.png"
            rest[3].save(str(ASSET_DIR / (preset_id + "_clef.png")))
            clef_name = preset_id + "_clef.png"
            if len(rest) >= 5:
                # 静态变体（五线谱）：去掉中段动态音符，供属性卡开关使用。
                _require_non_empty_asset(rest[4], preset_id + "_static")
                rest[4].save(str(ASSET_DIR / (preset_id + "_static.png")))
                static_name = preset_id + "_static.png"
        if not static_name:
            stale_static = ASSET_DIR / (preset_id + "_static.png")
            if stale_static.exists():
                stale_static.unlink()
        for stale_name in (preset_id + ".png", preset_id + ".gif"):
            stale = ASSET_DIR / stale_name
            if stale.exists():
                stale.unlink()
        PRESETS.append({"id": preset_id, "label": label, "image": frames[0],
                        "cap_l": rest[0], "cap_r": rest[1],
                        "height_pct": height_pct,
                        "tintable": tintable, "tint": default_tint,
                        "lines_name": lines_name, "clef_name": clef_name,
                        "static_name": static_name,
                        "animated": True})
        return
    if len(built) == 5:
        image, L, R, lines, clef_layer = built
        lines.save(str(ASSET_DIR / (preset_id + "_lines.png")))
        lines_name = preset_id + "_lines.png"
        clef_layer.save(str(ASSET_DIR / (preset_id + "_clef.png")))
        clef_name = preset_id + "_clef.png"
    elif len(built) == 4:
        image, L, R, lines = built
        lines.save(str(ASSET_DIR / (preset_id + "_lines.png")))
        lines_name = preset_id + "_lines.png"
        stale_clef = ASSET_DIR / (preset_id + "_clef.png")
        if stale_clef.exists():
            stale_clef.unlink()
    else:
        image, L, R = built
        lines_name = ""
        stale = ASSET_DIR / (preset_id + "_lines.png")
        if stale.exists():
            stale.unlink()
    if feather_px > 0:
        # 边缘羽化（用户口径 2026-10-24）：素材外缘不要硬描边，用透明度过渡。
        # 只羽化 alpha、不碰颜色；水平方向先做环形填充，保证羽化不破坏
        # 平铺的首尾衔接（否则每个块边界会多一条暗缝）。
        w, h = image.size
        pad = max(int(math.ceil(feather_px * 2)), 1)
        wrap = Image.new("RGBA", (w + 2 * pad, h), (0, 0, 0, 0))
        wrap.paste(image, (pad, 0))
        wrap.paste(image.crop((w - pad, 0, w, h)), (0, 0))
        wrap.paste(image.crop((0, 0, pad, h)), (pad + w, 0))
        alpha = wrap.split()[3].filter(
            ImageFilter.GaussianBlur(float(feather_px))
        ).crop((pad, 0, pad + w, h))
        image = image.copy()
        image.putalpha(alpha)
    image.save(str(ASSET_DIR / (preset_id + ".png")))
    PRESETS.append({"id": preset_id, "label": label, "image": image,
                    "cap_l": L, "cap_r": R, "height_pct": height_pct,
                    "tintable": tintable, "tint": default_tint,
                    "lines_name": lines_name, "clef_name": clef_name,
                    "animated": False})


def asset(preset):
    img = preset["image"]
    if preset["tint"]:
        return tint(img, preset["tint"])
    return img


def three_slice(src, cap_l, cap_r, band_w, band_h, mid_mode="repeat"):
    band_w, band_h = max(int(band_w), 1), max(int(band_h), 1)
    s = band_h / src.height
    left_w, right_w = cap_l * s, cap_r * s
    if left_w + right_w > band_w:
        s = band_w / max(cap_l + cap_r, 1)
        left_w, right_w = cap_l * s, cap_r * s
    left_w, right_w = int(round(left_w)), int(round(right_w))
    mid_w = max(band_w - left_w - right_w, 0)
    out = new(band_w, band_h)
    if cap_l:
        out.alpha_composite(src.crop((0, 0, cap_l, src.height))
                            .resize((max(left_w, 1), band_h), Image.LANCZOS), (0, 0))
    if cap_r:
        out.alpha_composite(src.crop((src.width - cap_r, 0, src.width, src.height))
                            .resize((max(right_w, 1), band_h), Image.LANCZOS),
                            (band_w - right_w, 0))
    if mid_w:
        mid = src.crop((cap_l, 0, src.width - cap_r, src.height))
        if mid_mode == "repeat":
            tw = mid.width
            tiles = int(math.ceil((mid_w / s) / tw)) + 2
            strip = new(tw * tiles)
            for i in range(tiles):
                strip.alpha_composite(mid, (i * tw, 0))
            strip = strip.resize((max(int(round(strip.width * s)), 1), band_h),
                                 Image.LANCZOS)
            phase = int(round(tw * s))
            out.alpha_composite(strip.crop((phase, 0, phase + mid_w, band_h)),
                                (left_w, 0))
        else:
            out.alpha_composite(mid.resize((mid_w, band_h), Image.LANCZOS), (left_w, 0))
    return out


def backdrop(w, h, top=(14, 17, 26), bottom=(30, 36, 52)):
    bg = Image.new("RGBA", (w, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        bg.paste(tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,),
                 (0, y, w, y + 1))
    glow_layer = new(w, h)
    gd = ImageDraw.Draw(glow_layer)
    gd.ellipse([-w * 0.2, -h * 0.5, w * 0.55, h * 0.6], fill=(70, 110, 190, 42))
    gd.ellipse([w * 0.6, h * 0.5, w * 1.2, h * 1.4], fill=(190, 90, 130, 34))
    bg.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(90)))
    return bg


LYRIC = "巡り会えたこの奇跡を"


def text_size(text, fnt):
    box = fnt.getbbox(text)
    return box[2] - box[0], box[3] - box[1]


def draw_lyric(canvas, text, cx, cy, size, dark=False):
    fnt = font(size)
    tw, th = text_size(text, fnt)
    d = ImageDraw.Draw(canvas)
    if dark:
        d.text((cx - tw / 2 + 2, cy - th / 2 + 3), text, font=fnt, fill=(255, 255, 255, 130))
        d.text((cx - tw / 2, cy - th / 2), text, font=fnt, fill=(26, 26, 30, 255))
    else:
        d.text((cx - tw / 2 + 2, cy - th / 2 + 3), text, font=fnt, fill=(0, 0, 0, 150))
        d.text((cx - tw / 2, cy - th / 2), text, font=fnt, fill=(255, 255, 255, 255))


DARK_TEXT_PRESETS = {"staff_scroll"}


def draw_still(canvas, x, y, w, h):
    """浅色"影片画面"底板：透明背景带要在浅色画面上才看得出效果。"""
    layer = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    layer.paste(vgrad(w, h, [(0, (198, 222, 246, 255)), (0.5, (232, 236, 226, 255)),
                             (1, (245, 228, 204, 255))]), (0, 0))
    gd = ImageDraw.Draw(layer)
    gd.ellipse([w * 0.02, h * 0.15, w * 0.42, h * 1.25], fill=(255, 248, 228, 170))
    gd.ellipse([w * 0.55, -h * 0.35, w * 0.98, h * 0.85], fill=(208, 230, 250, 150))
    layer = layer.filter(ImageFilter.GaussianBlur(16))
    canvas.paste(layer, (int(x), int(y)), round_mask(w, h, 18))


def sheet_gallery():
    pad, band_h = 60, 150
    lyric_px = 66
    row_h = 300
    w = 1560
    h = 120 + row_h * len(PRESETS) + 40
    canvas = backdrop(w, h)
    d = ImageDraw.Draw(canvas)
    d.text((pad, 40), "行背景带 · 预设参考 v10", font=font(40, True),
           fill=(255, 255, 255, 255))
    d.text((pad, 92), "五线谱=全透明+真高音谱号+项目音符轮廓（彩色）+阴影，配深色字/ 星火=素净 / 缎带·纯色条=可调色 / 水泡=纯蓝",
           font=font(20), fill=(176, 190, 214, 255))
    fnt = font(lyric_px)
    tw, _ = text_size(LYRIC, fnt)
    band_w = tw + 170
    for index, preset in enumerate(PRESETS):
        y = 150 + index * row_h
        tag = "  · 可调色 %s" % preset["tint"] if preset["tintable"] else ""
        d.text((pad, y), preset["label"] + tag, font=font(24, True),
               fill=(232, 240, 252, 255))
        d.text((pad, y + 34), "preset:%s   caps %d / %d   默认带高 %d%%" % (
            preset["id"], preset["cap_l"], preset["cap_r"], preset["height_pct"]),
            font=font(16), fill=(136, 152, 178, 255))
        band = three_slice(asset(preset), preset["cap_l"], preset["cap_r"],
                           band_w, band_h)
        bx, by = (w - band_w) // 2, y + 74
        if preset["id"] in DARK_TEXT_PRESETS:
            draw_still(canvas, bx - 70, by - 14, band_w + 140, band_h + 28)
        canvas.alpha_composite(band, (bx, by))
        draw_lyric(canvas, LYRIC, w / 2, by + band_h / 2, lyric_px,
                   dark=preset["id"] in DARK_TEXT_PRESETS)
    canvas.save(out_png("01_preset_gallery"))


def sheet_tints():
    palettes = ["#3EC6E0", "#FF7AA8", "#FFC24B", "#7C6BFF", "#5BD98B"]
    w, h = 1560, 700
    canvas = backdrop(w, h)
    d = ImageDraw.Draw(canvas)
    d.text((50, 36), "可调色预设 · 同一张中性图 × 用户颜色", font=font(36, True),
           fill=(255, 255, 255, 255))
    d.text((50, 84), "素材按灰度材质生产，运行时 multiply 上色；高光/阴影保留",
           font=font(19), fill=(176, 190, 214, 255))
    for row, preset in enumerate([p for p in PRESETS if p["tintable"]]):
        y = 140 + row * 260
        d.text((50, y), preset["label"], font=font(22, True), fill=(226, 236, 250, 255))
        for col, hexcolor in enumerate(palettes):
            color = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
            band = three_slice(tint(preset["image"], color), preset["cap_l"],
                               preset["cap_r"], 250, 96)
            x = 50 + col * 300
            canvas.alpha_composite(band, (x, y + 44))
            d.text((x, y + 150), hexcolor, font=font(15), fill=(150, 168, 196, 255))
    canvas.save(out_png("06_tints"))


def sheet_detail():
    cols = len(PRESETS)
    cell_w = 300
    w = 60 + cols * cell_w
    h = 690
    canvas = backdrop(w, h)
    d = ImageDraw.Draw(canvas)
    d.text((50, 36), "端点细节 2×", font=font(36, True), fill=(255, 255, 255, 255))
    for i, preset in enumerate(PRESETS):
        x = 50 + i * cell_w
        cap = asset(preset).crop((0, 0, preset["cap_l"], H))
        scale = min(2.0, (cell_w - 46) / preset["cap_l"])
        cap = cap.resize((int(preset["cap_l"] * scale), int(H * scale)), Image.LANCZOS)
        if preset["id"] in DARK_TEXT_PRESETS:
            draw_still(canvas, x, 120, cap.width, cap.height)
        canvas.alpha_composite(cap, (x, 120))
        d.text((x, 90), preset["label"], font=font(18), fill=(214, 226, 244, 255))
        d.text((x, 620), "左端点 %d px" % preset["cap_l"], font=font(15),
               fill=(140, 156, 182, 255))
    canvas.save(out_png("05_cap_detail"))


def sheet_assets():
    scale, pad, gap = 0.5, 40, 22
    items = []
    for preset in PRESETS:
        img = asset(preset)
        items.append((preset, img.resize((int(img.width * scale), int(H * scale)),
                                         Image.LANCZOS)))
    total_w = sum(item[1].width for item in items) + gap * (len(items) - 1) + pad * 2
    h = 360
    canvas = Image.new("RGBA", (total_w, h), (24, 27, 36, 255))
    cell = 16
    cd = ImageDraw.Draw(canvas)
    for yy in range(0, h, cell):
        for xx in range(0, total_w, cell):
            if (xx // cell + yy // cell) % 2 == 0:
                cd.rectangle([xx, yy, xx + cell - 1, yy + cell - 1],
                             fill=(30, 33, 44, 255))
    d = ImageDraw.Draw(canvas)
    x = pad
    for preset, img in items:
        canvas.alpha_composite(img, (x, 70))
        d.text((x, 30), preset["id"], font=font(18, True), fill=(235, 242, 252, 255))
        d.text((x, 200), "caps %d / %d" % (preset["cap_l"], preset["cap_r"]),
               font=font(15), fill=(150, 168, 196, 255))
        for cap_x, color in ((preset["cap_l"] * scale, (120, 210, 255, 220)),
                             (img.width - preset["cap_r"] * scale, (255, 160, 190, 220))):
            for yy in range(64, 190, 8):
                d.line([(x + cap_x, yy), (x + cap_x, yy + 4)], fill=color, width=1)
        x += img.width + gap
    canvas.save(out_png("00_assets"))


def sheet_variation():
    preset = PRESETS[0]
    lines = ["夜を越えて", "巡り会えたこの奇跡を", "この声が届くまで歌い続けるよ"]
    pad, band_h, lyric_px = 60, 132, 62
    row_h = 224
    w = 1560
    h = 150 + row_h * len(lines)
    canvas = backdrop(w, h)
    d = ImageDraw.Draw(canvas)
    d.text((pad, 40), "同一预设 · 不同行长（三段式）", font=font(38, True),
           fill=(255, 255, 255, 255))
    d.text((pad, 92), "左右端点固定不变形，中段按需重复", font=font(20),
           fill=(176, 190, 214, 255))
    fnt = font(lyric_px)
    for index, text in enumerate(lines):
        y = 150 + index * row_h
        tw, _ = text_size(text, fnt)
        band_w = tw + 190
        band = three_slice(asset(preset), preset["cap_l"], preset["cap_r"],
                           band_w, band_h)
        canvas.alpha_composite(band, ((w - band_w) // 2, y + 44))
        draw_lyric(canvas, text, w / 2, y + 44 + band_h / 2, lyric_px, dark=True)
    canvas.save(out_png("02_width_variation"))


def sheet_explained():
    preset = PRESETS[0]
    src = asset(preset)
    pad = 50
    w, h = 1560, 760
    canvas = backdrop(w, h)
    d = ImageDraw.Draw(canvas)
    d.text((pad, 34), "三段式怎么切", font=font(38, True), fill=(255, 255, 255, 255))
    d.text((pad, 86), "左右端点按纵向比例缩放、不变形；中段裁出来单独平铺",
           font=font(20), fill=(176, 190, 214, 255))
    sw = src.width
    s_img = src.copy()
    overlay = new(sw)
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, 0, preset["cap_l"], H], fill=(90, 200, 255, 46))
    od.rectangle([sw - preset["cap_r"], 0, sw, H], fill=(255, 140, 170, 46))
    od.rectangle([preset["cap_l"], 0, sw - preset["cap_r"], H], fill=(180, 255, 160, 30))
    for x in (preset["cap_l"], sw - preset["cap_r"]):
        od.line([(x, 0), (x, H)], fill=(255, 255, 255, 190), width=2)
    s_img.alpha_composite(overlay)
    sx = (w - sw) // 2
    canvas.alpha_composite(s_img, (sx, 150))
    d.text((sx + preset["cap_l"] / 2 - 46, 128), "左端点 %d" % preset["cap_l"],
           font=font(16), fill=(150, 220, 255, 255))
    d.text((sx + sw - preset["cap_r"] - 100, 128), "右端点 %d" % preset["cap_r"],
           font=font(16), fill=(255, 174, 196, 255))
    d.text((sx + sw / 2 - 70, 128), "中段（循环单元）", font=font(16),
           fill=(196, 255, 180, 255))
    d.text((pad, 430), "同一张图，铺到不同宽度：", font=font(22, True),
           fill=(226, 236, 250, 255))
    for index, (bw, label) in enumerate([(420, "420 px"), (760, "760 px"),
                                         (1180, "1180 px")]):
        bh = 112
        band = three_slice(src, preset["cap_l"], preset["cap_r"], bw, bh)
        by = 474 + index * 96
        canvas.alpha_composite(band, (pad + 120, by))
        d.text((pad, by + bh / 2 - 12), label, font=font(18), fill=(150, 168, 196, 255))
    canvas.save(out_png("03_three_slice_explained"))


def bright_backdrop(w, h):
    """偏《吹响吧！上低音号》的清爽画面：天光 + 暖色 + 校园感。"""
    bg = Image.new("RGBA", (w, h))
    bg.paste(vgrad(w, h, [(0, (188, 218, 246, 255)), (0.45, (226, 234, 224, 255)),
                          (1, (247, 229, 203, 255))]), (0, 0))
    glow_layer = new(w, h)
    gd = ImageDraw.Draw(glow_layer)
    gd.ellipse([-w * 0.15, h * 0.18, w * 0.5, h * 1.15], fill=(255, 248, 226, 185))
    gd.ellipse([w * 0.52, -h * 0.28, w * 1.15, h * 0.75], fill=(255, 232, 208, 150))
    gd.ellipse([w * 0.1, h * 0.6, w * 0.85, h * 1.35], fill=(206, 228, 206, 120))
    bg.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(120)))
    return bg


def sheet_mock():
    w, h = 1920, 1080
    canvas = bright_backdrop(w, h)
    canvas = canvas.convert("RGB").filter(ImageFilter.GaussianBlur(3)).convert("RGBA")
    d = ImageDraw.Draw(canvas)
    text1, preset1, band_h, cy = "巡り会えたこの奇跡を", PRESETS[0], 150, 430
    fnt = font(76)
    tw, _ = text_size(text1, fnt)
    band_w = tw + 190
    band = three_slice(asset(preset1), preset1["cap_l"], preset1["cap_r"],
                       band_w, band_h)
    canvas.alpha_composite(band, (int(w / 2 - band_w / 2), int(cy - band_h / 2)))
    draw_lyric(canvas, text1, w / 2, cy, 76, dark=True)

    text2, preset2, cy2 = "ずっと歌い続けるよ", PRESETS[3], 700
    tw2, _ = text_size(text2, fnt)
    band_w2 = tw2 + 170
    base_h = int(76 * 1.2 * 1.25)
    top = min(int(cy2 - base_h / 2), cy2 - 94)
    band_h2 = int(cy2 + base_h / 2) - top
    band2 = three_slice(asset(preset2), preset2["cap_l"], preset2["cap_r"],
                        band_w2, band_h2)
    canvas.alpha_composite(band2, (int(w / 2 - band_w2 / 2), top))
    draw_lyric(canvas, text2, w / 2, cy2, 76)
    rw, _ = text_size("ずっと", font(30))
    d.text((w / 2 - rw / 2, cy2 - 74), "ずっと", font=font(30), fill=(255, 214, 228, 240))
    d.text((40, 40), "mock · 1920x1080（浅色画面里看透明五线）", font=font(22),
           fill=(70, 78, 92, 220))
    canvas.convert("RGB").save(out_png("04_mock_frame"))


def seam_report(preset):
    img = preset["image"]
    a = np.array(img.split()[3]).astype(np.float32)
    L, R = preset["cap_l"], preset["cap_r"]
    tw = img.width - L - R
    seam = np.abs(a[:, L + tw - 1] - a[:, L]).mean()
    natural = np.abs(a[:, L + 1] - a[:, L]).mean()
    ratio = seam / natural if natural > 0.4 else (0.0 if seam < 0.4 else 9.9)
    return ratio


def main() -> None:
    """生成全部预设素材与评审图（直接运行本脚本时执行）。"""
    register("staff_scroll", "五线谱 · 卷轴", build_staff_scroll, 160, animated=True)
    register("stars", "星火点阵", build_stars, 110, animated=True)
    register("diagonal", "斜纹缎带", build_diagonal, 100, True, "#3EC6E0", feather_px=4.0)
    register("bubbles", "水泡", build_bubbles, 125, animated=True)
    register("plain_round", "纯色圆角条", build_plain_round, 100, True, "#2B3A55", feather_px=4.0)

    sheet_assets()
    sheet_gallery()
    sheet_variation()
    sheet_explained()
    sheet_tints()
    sheet_detail()
    sheet_mock()

    for preset in PRESETS:
        ratio = seam_report(preset)
        # 随机散布类预设（星火/水泡）的"边缘列"本来就稀疏，这个指标对它们没有意义
        structured = preset["id"] in {"staff_scroll", "diagonal", "plain_round"}
        flag = "  <-- 相位不连续，平铺会跳" if (structured and ratio > 1.6) else ""
        print("%-13s %s caps %d/%d  seam-phase %.2f%s" % (
            preset["id"], preset["image"].size, preset["cap_l"], preset["cap_r"],
            ratio, flag))
    print("done:", OUT)


if __name__ == "__main__":
    main()















