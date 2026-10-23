# -*- coding: utf-8 -*-
"""行背景带预设素材生成器（开发用，不参与打包）。

产出：
  1. 五张预设 PNG -> krok_helper/assets/subtitle_render/band_presets/
  2. 评审用预览图（7 张）-> %TEMP%/lin-k-band-preview/

依赖：Pillow + numpy（仅开发机需要，运行时不用）。
用法：
  python scripts/make_band_presets.py
  $env:BAND_PREVIEW_TAG="v11"; python scripts/make_band_presets.py
换 TAG 只是为了换预览文件名、避开看图端的缓存；素材文件本身名字固定。
"""

import math
import os
import random
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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


def paste_note(target, head_x, head_y, em_px, color, commands):
    sprite, anchor = note_sprite(em_px, color, commands)
    target.alpha_composite(sprite, (int(head_x - anchor[0]), int(head_y - anchor[1])))


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
    """全透明五线：真高音谱号开篇 + 项目音符轮廓（彩色）+ 柔和投影。

    2026-10-23 第二轮（用户口径）：
    * **谱线与高音谱号同属一层**（``staff_scroll_lines.png``，白色），运行时
      multiply 上色——谱线颜色改了，谱号一起改；
    * 谱号自带**深色描边**（描边烘焙成近黑，multiply 时黑永远是黑），所以
      上色后仍压得住谱线；
    * 图层顺序：谱线层（含谱号）在下、音符与终止线在上；素材本体不再画
      谱号和谱线，避免重复/压字；
    * 中段 4096px：16:9 画布正常歌词行不会触发第二次平铺。
    """
    w, L, R = 190 + 4096 + 160, 190, 160
    content = new(w)
    commands = load_note_commands() or []

    # 左端点：两个彩色音符，全部位于五线谱上方（用户口径 2026-10-24）
    cap = new(L)
    if commands:
        paste_note(cap, 108, 52, 56, NOTE_COLORS[0], commands)
        paste_note(cap, 146, 40, 44, NOTE_COLORS[1], commands)
    content.alpha_composite(cap, (0, 0))

    # 右端点：两个彩色音符（同样在谱线上方）；终止竖线移到谱线层，
    # 跟着谱线颜色一起变（用户口径 2026-10-24）。
    cap = new(R)
    if commands:
        paste_note(cap, 40, 52, 56, NOTE_COLORS[2], commands)
        paste_note(cap, 82, 40, 44, NOTE_COLORS[3], commands)
    content.alpha_composite(cap, (w - R, 0))

    # 只保留阴影
    shadow = content.split()[3].filter(ImageFilter.GaussianBlur(3.2))
    sh = Image.new("RGBA", (w, H), (12, 12, 16, 0))
    sh.putalpha(shadow.point(lambda v: int(v * 0.62)))
    img = new(w)
    img.alpha_composite(sh, (2, 3))
    img.alpha_composite(content)

    # 谱线层：5 条白线 + 白色谱号（带近黑描边）。运行时 multiply 用户颜色：
    # 白 → 颜色，近黑描边保持近黑。
    lines = new(w)
    ld = ImageDraw.Draw(lines)
    for y in (64, 92, 120, 148, 176):
        ld.line([(0, y), (w, y)], fill=(255, 255, 255, 255), width=3)
    clef = clef_sprite(
        140, (255, 255, 255), outline=(18, 18, 20), outline_width=9
    )
    if clef is not None:
        lines.alpha_composite(clef, (10, 120 - clef.height // 2))
    # 终止竖线（双小节线）属于五线谱：白色画进谱线层，multiply 后随谱线色。
    ld.line([(w - 13, 50), (w - 13, 190)], fill=(255, 255, 255, 255), width=3)
    ld.line([(w - 3, 50), (w - 3, 190)], fill=(255, 255, 255, 255), width=7)
    return img, L, R, lines


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
        r = 1.6 + rnd.random() ** 2 * 3.2
        put_wrapped(big, tw, rnd.random() * tw, 30 + rnd.random() * (H - 60),
                    clean_star(r, int(130 + rnd.random() * 110)))


def build_stars():
    w, L, R = 120 + 4096 + 120, 120, 120
    img = new(w)
    body = vgrad(w, H, [(0, (20, 24, 40, 130)), (0.5, (14, 17, 30, 96)),
                        (1, (8, 10, 20, 150))])
    mask = round_mask(w, H, 32)
    tmp = new(w)
    tmp.paste(body, (0, 0))
    img.alpha_composite(Image.composite(tmp, new(w), mask))
    img.alpha_composite(draw_tile(w - L - R, _stars_mid), (L, 0))
    for side, seed in ((0, 3), (1, 4)):
        rnd = random.Random(seed)
        cap = new(L if side == 0 else R)
        for _ in range(7):
            r = 1.8 + rnd.random() ** 1.8 * 4.4
            spr = clean_star(r, int(150 + rnd.random() * 100))
            cap.alpha_composite(spr, (int(rnd.random() * (cap.width - spr.width)),
                                      int(30 + rnd.random() * (H - 60 - spr.height))))
        if side:
            cap = cap.transpose(Image.FLIP_LEFT_RIGHT)
        img.alpha_composite(cap, (0 if side == 0 else w - R, 0))
    bevel(img, top=30, bottom=54, height=6)
    return img, L, R


# ------------------------------------- 3. neutral satin ribbon (tintable)
def build_diagonal():
    w, L, R = 96 + 4080 + 96, 96, 96
    tw = w - L - R                      # 4080; stripe period MUST divide it
    period = 40
    assert tw % period == 0, "middle tile width must be a multiple of the stripe period"

    body = vgrad(w, H, [(0, (252, 252, 255, 253)), (0.14, (214, 218, 226, 253)),
                        (0.45, (168, 174, 186, 253)), (0.78, (126, 132, 144, 253)),
                        (1, (92, 96, 106, 255))])
    img = new(w)
    img.paste(body, (0, 0))

    stripes = new(w)
    sd = ImageDraw.Draw(stripes)
    for k in range(-14, w // period + 20):
        x0 = k * period
        sd.polygon([(x0, 0), (x0 + 18, 0), (x0 + 18 - H, H), (x0 - H, H)],
                   fill=(255, 255, 255, 64))
        sd.polygon([(x0, 0), (x0 + 4, 0), (x0 + 4 - H, H), (x0 - H, H)],
                   fill=(255, 255, 255, 104))
    img.alpha_composite(stripes)

    mask = round_mask(w, H, 28)
    img = Image.composite(img, new(w), mask)

    # 端点折边：中段是"整块重复"，最后一块必然是半块，条纹会在端点边界相位跳。
    # 与其掩盖，不如两边各加一道折边（高光 + 暗线），让条纹在那里"收口"。
    fold = new(w)
    fd = ImageDraw.Draw(fold)
    # 折边必须落在"端点里"，不能压到中段的循环单元上，否则每铺一块就多一道线。
    fd.rectangle([L - 12, 0, L - 3, H], fill=(20, 20, 24, 90))
    fd.rectangle([L - 3, 0, L - 1, H], fill=(255, 255, 255, 130))
    fd.rectangle([w - R, 0, w - R + 2, H], fill=(255, 255, 255, 130))
    fd.rectangle([w - R + 2, 0, w - R + 12, H], fill=(20, 20, 24, 90))
    fold = fold.filter(ImageFilter.GaussianBlur(1.2))
    fold.putalpha(ImageChops.multiply(fold.split()[3], mask))
    img.alpha_composite(fold)

    shade = hgrad(w, H, [(0, (40, 44, 52, 150)), (0.12, (40, 44, 52, 0)),
                         (0.88, (40, 44, 52, 0)), (1, (40, 44, 52, 150))])
    img.alpha_composite(Image.composite(shade, new(w), mask))
    bevel(img, top=72, bottom=96, height=6)
    edge = new(w)
    ImageDraw.Draw(edge).line([(0, 2), (w, 2)], fill=(255, 255, 255, 110), width=2)
    edge.putalpha(ImageChops.multiply(edge.split()[3], mask))
    img.alpha_composite(edge)
    return img, L, R


# ------------------------------------------------------ 4. clean blue bubbles
BLUES = [(150, 226, 255), (196, 240, 255), (118, 196, 236), (168, 255, 240)]


def bubble_sprite(r, color):
    n = int(r * 3.2) | 1
    yy, xx = np.mgrid[0:n, 0:n]
    c = (n - 1) / 2
    dist = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / max(r, 1e-3)
    rim = np.exp(-((dist - 0.9) / 0.11) ** 2)
    inner = np.clip(1 - dist, 0, 1) * 0.20
    a = np.clip((rim * 0.88 + inner) * 255, 0, 255)
    arr = np.zeros((n, n, 4), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = color
    arr[..., 3] = a.astype(np.uint8)

    halo = radial(int(n * 1.8), color + (58,), 2.2)
    out = new(int(n * 1.8), int(n * 1.8))
    out.alpha_composite(halo)
    ox = (out.width - n) // 2
    oy = (out.height - n) // 2
    out.alpha_composite(Image.fromarray(arr, "RGBA"), (ox, oy))
    d = ImageDraw.Draw(out)
    d.ellipse([ox + c - r * 0.54, oy + c - r * 0.60,
               ox + c - r * 0.30, oy + c - r * 0.36], fill=(255, 255, 255, 235))
    return out.filter(ImageFilter.GaussianBlur(0.6))


def _caustics(big, tw):
    """焦散：水面折光在地板上那种摇曳的亮线（跨接缝连续）。"""
    rnd = random.Random(88)
    d = ImageDraw.Draw(big)
    for _ in range(9):
        y0 = 18 + rnd.random() * (H - 36)
        amp = 5 + rnd.random() * 13
        wave = 50 + rnd.random() * 70
        phase = rnd.random() * math.tau
        width = 2 if rnd.random() < 0.7 else 3
        alpha = int(26 + rnd.random() * 34)
        pts = [(x, y0 + amp * math.sin(x / wave * math.tau + phase))
               for x in range(-40, tw * 3 + 40, 5)]
        d.line(pts, fill=(198, 244, 255, alpha), width=width)
    return big.filter(ImageFilter.GaussianBlur(1.4))


def _bubbles_mid(big, tw):
    rnd = random.Random(29)
    count = max(1, int(round(tw * 18 / 320)))
    for _ in range(count):
        r = 4 + rnd.random() ** 1.6 * 18
        put_wrapped(big, tw, rnd.random() * tw, 26 + rnd.random() * (H - 52),
                    bubble_sprite(r, rnd.choice(BLUES)))


def build_bubbles():
    """海水：深青渐变 + 焦散亮线 + 水面光柱 + 气泡（《凪のあすから》那种感觉）。"""
    w, L, R = 160 + 4080 + 160, 160, 160
    tw = w - L - R
    img = new(w)

    ends = round_mask(w, H, 30)
    feather = vgrad(w, H, [(0, (255, 255, 255, 0)), (0.08, (255, 255, 255, 255)),
                           (0.92, (255, 255, 255, 255)), (1, (255, 255, 255, 0))])
    fcol = np.array(feather.split()[3]).astype(np.float32)[:, 0] / 255.0

    def shaped_full(layer):
        arr = np.array(layer.split()[3]).astype(np.float32) * fcol[:, None]
        out = layer.copy()
        out.putalpha(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
        out.putalpha(ImageChops.multiply(out.split()[3], ends))
        return out

    def shaped_tile(layer):
        arr = np.array(layer.split()[3]).astype(np.float32) * fcol[:, None]
        out = layer.copy()
        out.putalpha(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
        return out

    water = vgrad(w, H, [(0, (104, 198, 212, 150)), (0.24, (40, 136, 164, 158)),
                         (0.62, (14, 74, 104, 176)), (1, (4, 24, 42, 200))])
    img.alpha_composite(shaped_full(water))

    # 水面光柱：只放端点，避免中段平铺重复
    for side, seed in ((0, 21), (1, 22)):
        rays = new(L if side == 0 else R)
        rd = ImageDraw.Draw(rays)
        rnd = random.Random(seed)
        for _ in range(5):
            x0 = rnd.random() * rays.width
            spread = 26 + rnd.random() * 54
            lean = 18 + rnd.random() * 26
            rd.polygon([(x0, 0), (x0 + spread, 0),
                        (x0 + spread - lean, H), (x0 - lean, H)],
                       fill=(214, 248, 255, int(16 + rnd.random() * 18)))
        rays = shaped_full(rays.filter(ImageFilter.GaussianBlur(9)))
        img.alpha_composite(rays, (0 if side == 0 else w - R, 0))

    img.alpha_composite(shaped_tile(draw_tile(tw, _caustics)), (L, 0))
    img.alpha_composite(shaped_tile(draw_tile(tw, _bubbles_mid)), (L, 0))

    for side, seed in ((0, 8), (1, 9)):
        rnd = random.Random(seed)
        cap = new(L if side == 0 else R)
        for _ in range(9):
            r = 4 + rnd.random() ** 1.4 * 19
            spr = bubble_sprite(r, rnd.choice(BLUES))
            cap.alpha_composite(spr, (int(rnd.random() * (cap.width - spr.width)),
                                      int(22 + rnd.random() * (H - 44 - spr.height))))
        if side:
            cap = cap.transpose(Image.FLIP_LEFT_RIGHT)
        img.alpha_composite(cap, (0 if side == 0 else w - R, 0))
    return img, L, R


# -------------------------------------- 5. neutral glass pill (tintable)
def build_plain_round():
    w, L, R = 64 + 4096 + 64, 64, 64
    body = vgrad(w, H, [(0, (250, 251, 255, 236)), (0.2, (206, 210, 220, 240)),
                        (0.62, (146, 150, 162, 244)), (1, (96, 100, 110, 248))])
    mask = round_mask(w, H, 30)
    img = new(w)
    img.paste(body, (0, 0), mask)
    sheen = vgrad(w, H, [(0, (255, 255, 255, 120)), (0.07, (255, 255, 255, 40)),
                         (0.2, (255, 255, 255, 0))])
    img.alpha_composite(Image.composite(sheen, new(w), mask))
    border = new(w)
    ImageDraw.Draw(border).rounded_rectangle([0, 0, w - 1, H - 1], radius=30,
                                             outline=(255, 255, 255, 255), width=2)
    arr = np.array(border).astype(np.float32)
    ys = np.arange(arr.shape[0])[:, None] / (arr.shape[0] - 1)
    arr[..., 3] = np.clip(arr[..., 3] * (0.30 + 0.45 * (1 - ys)), 0, 255)
    img.alpha_composite(Image.fromarray(arr.astype(np.uint8), "RGBA"))
    shadow = new(w)
    ImageDraw.Draw(shadow).rounded_rectangle([0, 0, w - 1, H - 1], radius=30,
                                             fill=(0, 0, 0, 255))
    arr = np.array(shadow).astype(np.float32)
    arr[..., 3] = np.clip(arr[..., 3] * np.clip((ys - 0.86) / 0.14, 0, 1) * 0.5, 0, 255)
    img.alpha_composite(Image.fromarray(arr.astype(np.uint8), "RGBA")
                        .filter(ImageFilter.GaussianBlur(4)))
    return img, L, R


# ---------------------------------------------------------------- assembly
PRESETS = []


def register(preset_id, label, builder, height_pct, tintable=False, default_tint=None):
    built = builder()
    if len(built) == 4:
        image, L, R, lines = built
        lines.save(str(ASSET_DIR / (preset_id + "_lines.png")))
        lines_name = preset_id + "_lines.png"
    else:
        image, L, R = built
        lines_name = ""
        stale = ASSET_DIR / (preset_id + "_lines.png")
        if stale.exists():
            stale.unlink()
    image.save(str(ASSET_DIR / (preset_id + ".png")))
    PRESETS.append({"id": preset_id, "label": label, "image": image,
                    "cap_l": L, "cap_r": R, "height_pct": height_pct,
                    "tintable": tintable, "tint": default_tint,
                    "lines_name": lines_name})


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


register("staff_scroll", "五线谱 · 卷轴", build_staff_scroll, 160)
register("stars", "星火点阵", build_stars, 110)
register("diagonal", "斜纹缎带", build_diagonal, 100, True, "#3EC6E0")
register("bubbles", "水泡", build_bubbles, 125)
register("plain_round", "纯色圆角条", build_plain_round, 100, True, "#2B3A55")

sheet_assets()
sheet_gallery()
sheet_variation()
sheet_explained()
sheet_tints()
sheet_detail()
sheet_mock()


def seam_report(preset):
    img = preset["image"]
    a = np.array(img.split()[3]).astype(np.float32)
    L, R = preset["cap_l"], preset["cap_r"]
    tw = img.width - L - R
    seam = np.abs(a[:, L + tw - 1] - a[:, L]).mean()
    natural = np.abs(a[:, L + 1] - a[:, L]).mean()
    ratio = seam / natural if natural > 0.4 else (0.0 if seam < 0.4 else 9.9)
    return ratio


for preset in PRESETS:
    ratio = seam_report(preset)
    # 随机散布类预设（星火/水泡）的"边缘列"本来就稀疏，这个指标对它们没有意义
    structured = preset["id"] in {"staff_scroll", "diagonal", "plain_round"}
    flag = "  <-- 相位不连续，平铺会跳" if (structured and ratio > 1.6) else ""
    print("%-13s %s caps %d/%d  seam-phase %.2f%s" % (
        preset["id"], preset["image"].size, preset["cap_l"], preset["cap_r"],
        ratio, flag))
print("done:", OUT)















