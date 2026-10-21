"""入场/退场/唱字装饰粒子：sprite 常量、burst 规划器与轨迹求值器。

设计约束（与 C++ ``d2d_backend_render.cpp`` 的粒子求值互为镜像）：

- 所有随机量经 :func:`transitions.fx_unit_hash` 的同源整数哈希派生，轨迹是
  时间的纯函数（无状态累积），QPainter 与 D2D 两条后端逐帧一致、导出可复现。
- burst 由 Python 侧规划（``plan_line_bursts`` 同一函数供渲染 IR 与 Painter
  调用），锚点坐标由各后端按自身布局解析（行中心 / 字符 pivot），布局一致性
  由既有双后端布局奇偶校验保证。
- sprite 轮廓是本模块定义的常量（M/L/C/Q/Z，1000 单位 em 空间、以 (0,0) 为
  中心），随场景 IR 下发（``fx_sprites``），C++ 不内置副本——单一事实源。
- 粒子颜色模式（默认颜色·樱花粉双色 / 单独颜色·双色槽 / 跟随字体·
  前后各一 / 跟随字体·走字前后 / 复用配色方案）在规划期解析成每 burst
  的实色（#RRGGBB）下发，两条后端按 burst 颜色实心绘制、天然同色；
  双色档由规划器拆成两条同轨迹、不同种子/半数量的 burst 实现「每颗粒
  子随机取一色」，渲染端无特殊分支；单色跟随档（走字前/后、role）另带
  完整 PaintFill 装饰规格（与音符「跟随字体」同源）。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass

from krok_helper.subtitle_render.domain.models import (
    Style,
    normalize_glow_concentration_level,
)
from krok_helper.subtitle_render.domain.paint import KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    decoration_kind_for_state,
    glow_concentration_level,
    shadow_offset_x,
    shadow_offset_y,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    appearance_role_source,
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.serialization.paint import paint_fill_to_dict
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    fx_unit_hash,
)

# 编排时长（ms）；改动需与 C++ 镜像同步。
SPARKLE_ENTRY_LIFE_MS = 700
SPARKLE_EXIT_LIFE_MS = 600
RIPPLE_LIFE_MS = 600
# 水波纹每个落点的圈数与错峰间隔（Aegisub/AE 同款：细环 2-3 圈先后扩散）。
RIPPLE_RING_COUNT = 3
RIPPLE_RING_STAGGER_MS = 150
# 唱字粒子的发射窗 + 最长寿命：不受字符唱字窗口约束（用户口径：在正确
# 时间正确地点发射、以合适的感觉播完即可），保证 twinkle/note 完整走完
# 轨迹而不是在换字瞬间被硬截断。
SING_TWINKLE_TOTAL_MS = 700
SING_NOTE_TOTAL_MS = 800
# 涟漪每个字的逐字错峰（与 char_fade / 几何入退场的 350ms 编排同构）。
RIPPLE_CHAR_STAGGER_MS = 350
# 粒子拼接/消散（assemble / dissolve）：每字飞入/飞离的行程与寿命。
ASSEMBLE_TRAVEL_EM = 2.2
ASSEMBLE_LIFE_MS = 320
# 花瓣（樱花飘落，2026-10）：飘入/飘散/飘动三档共用一条「\moves4 四点飘移」
# 等效轨迹（sin 横向摇摆 + 单调竖直行程 + 连续翻转）。位置偏移全部取
# box_w/box_h 比例（星光/音符同约定），不带字号倍数行程（2026-10 用户
# 口径：初版 travel=1.6em 出生过高、飘进行间距）。
PETAL_LIFE_MS = 900
# 「默认颜色」档的两种樱花粉：规划器拆双 burst 随机混发（每颗粒子一色）。
SAKURA_PINK_A = "#FFB7C5"
SAKURA_PINK_B = "#FFD7E0"
# 雪花（2026-10，与花瓣同管线）：六角星 sprite + 慢速匀速飘落的运动签名
#（宽幅低频摇摆、慢自转、柔包络——AE CC Snowfall 等参考的共同观感）；
# 寿命比花瓣长（下落更慢）。
SNOW_LIFE_MS = 1100
# 雪花出入场的标准整行窗（2026-10 分离口径：旧「入场/退场动画时长」
# 旋钮默认 600ms 冻结为粒子发射标准；旋钮此后只控制文字逐字渐显/渐隐）。
SNOW_STD_WINDOW_MS = 600
# 雪花出入场下落窗（2026-10-18 用户口径：雪下得太快、过半字就看不见）：
# 入场提前 350ms 起飘、整段落 1800ms；退场落窗同样 1800ms——慢速终端
# 速度感，且淡出延后到接近字底，保证至少落到主文字最底部。
SNOW_ENTRY_LEAD_MS = 350
SNOW_ENTRY_FALL_MS = 1800
SNOW_EXIT_FALL_MS = 1800
# 雪花/花瓣“落到底再淡出”的包络：前 78%/75% 保持不透明，最后一段
# 才收透明度（旧版 p=0.5 已 α≈0.4，视觉上像半途消失）。
SNOW_FADE_START = 0.78
PETAL_FADE_START = 0.75
# 「默认颜色」档的雪花双色（雪白 + 冰蓝）随机混发；固定默认档按 kind
# 取默认对（雪花=雪色，花瓣与其余 kind=樱花粉，见 _default_color_pair）。
SNOW_A = "#F2F8FF"
SNOW_B = "#BFDFFF"
# 羽毛（2026-10，AE 羽毛参考）：羽绒质感只靠轮廓（羽轴 + 两侧羽枝缺口）
# 与运动区分——宽幅低频钟摆横摆 + 连续翻转 + 边缘朝相机时的横向压扁
#（stretch_x 0.55~1.0），落点压到字底后淡出；寿命比花瓣长（飘得更慢）。
FEATHER_LIFE_MS = 1700
FEATHER_FADE_START = 0.80
# 「默认颜色」档的羽毛双色：纯白 + 暖米白（深色视频上分层，且区别于
# 雪花的冷冰蓝）。
FEATHER_A = "#FFFFFF"
FEATHER_B = "#FFF1D6"
# 闪电（2026-10，AE Advanced Lightning 参考）：分叉锯齿 bolt + 极短
# 单次放电（100~240ms）+ 窗口内多次随机重击 + 高频频闪；核心 bolt 前面
# 绘制，同轨迹放大 1.35× 的暗色 glow 垫在文字后当作辉光。
LIGHTNING_WINDOW_MS = 520
LIGHTNING_SIZE_SCALE = 3.2
LIGHTNING_GLOW_SIZE_SCALE = 1.35
LIGHTNING_GLOW_ALPHA = 0.22
# 出入场每字窗口内的重击次数（AE 闪电的多次放电感；双色变体会对半分）。
# 7 次/520ms ≈ 13.5 次/秒：低于此值在 5 秒采样里会出现明显“空窗”。
ANIM_LIGHTNING_STRIKES = 7
# 默认双色：冷白青 + 电光蓝。
LIGHTNING_A = "#F2FDFF"
LIGHTNING_B = "#6FD3FF"
# 闪电 bolt 的多边形元素半宽（1000 em 空间）：主干两端的收尖与中段加宽。
_LIGHTNING_TRUNK = (
    (0.0, -500.0),
    (-72.0, -320.0),
    (26.0, -170.0),
    (-44.0, 20.0),
    (58.0, 220.0),
    (-8.0, 500.0),
)
_LIGHTNING_HALF_WIDTHS = (14.0, 40.0, 38.0, 44.0, 40.0, 20.0)
# 分叉：(起点, 终点, 起点半宽, 终点半宽)；起点落在主干内部保证并集连通。
_LIGHTNING_BRANCHES = (
    ((-52.0, -292.0), (-220.0, -210.0), 26.0, 8.0),
    ((-58.0, -282.0), (-252.0, -330.0), 20.0, 6.0),
    ((4.0, -96.0), (205.0, -25.0), 24.0, 7.0),
    ((22.0, 140.0), (-205.0, 235.0), 22.0, 6.0),
)
# 雪花两档（2026-10 用户口径）：``snow`` = 教程并集轮廓带全部洞环（界面
# 「雪花飘入/飘出/飘动·镂空」）；``snow_solid`` = **0890458d 时代的 ❄ 枝晶
# 星形**（「…·实心」，2026-10-07 用户指定保留当时形状，不是镂空档去洞环）。
# 运动签名、配色与出入场时序完全同源，只有 sprite 不同——规划器/求值器里
# 所有按 snow 分流的判断都必须用本集合，别写成单值比较。
SNOW_KINDS = frozenset({"snow", "snow_solid"})
# 「默认颜色」档的爱心双色（2026-10 用户口径：红 + 粉；爱心档全面克隆
# 音符飘出的三档特效，只换形状与默认色）。取自 canvify 情人节配色表：
# Classic Valentine Red #E63946 + Blush Pink #F9C2D1——小尺寸实心剪影下
# 红/粉明度差最大、互不混色；深度红（#C8102E 一族）在深色视频上读暗，弃用。
HEART_A = "#E63946"
HEART_B = "#F9C2D1"
# 火焰 v3 色阶（2026-10-18 用户口径：v2 仍不像「酷炫燃系」；联网研究
# Aegisub 火焰模板 + 实时 VFX/UE5 三层火后重做）：
#   - 单颗粒子只承担一层色；整束火由**四层色阶**叠出白热芯 → 金 → 橙 →
#     红边（标准火色阶：白 #FFF7D6 → 金 #FFD24A → 橙 #FFA31A →
#     红边 #FF6B1A；Aeg 模板的 HSV 30–56° 黄橙随机是同一段色域的抖动）。
#   - 层间尺寸/行程递减（外大慢、芯小快），火舌外扩、火芯短亮，读作
#     有体积的火焰而不是同尺寸贴纸。
#   - FIRE_A/FIRE_B 保留两端色给旧 IR / 默认双色接口兼容。
FIRE_CORE = "#FFF7D6"
FIRE_INNER = "#FFD24A"
FIRE_OUTER = "#FFA31A"
FIRE_EDGE = "#FF6B1A"
FIRE_A = FIRE_EDGE
FIRE_B = FIRE_CORE
# 四层火色阶：(颜色, 尺寸倍率, 行程倍率, sprite kind)。外两层用主火团
# （flame）、内两层用细长内焰（flame_alt）——同尺寸层叠时不会读成同一
# 张剪影复制粘贴，火芯有独立的细长形态。
FIRE_LAYERS = (
    (FIRE_EDGE, 1.00, 1.00, "flame"),
    (FIRE_OUTER, 0.88, 0.94, "flame"),
    (FIRE_INNER, 0.70, 0.88, "flame_alt"),
    (FIRE_CORE, 0.50, 0.80, "flame_alt"),
)
# 火焰粒子寿命：火苗有余韵，比音符长、与雪花同档（Burning 模板的火粒
# 淡出窗口 2000ms 中实际可见约 1s）。
FLAME_LIFE_MS = 1100
# 唱字持续喷发：字窗内每 ~280ms 补一簇（Aeg 模板 maxloop 每 1000ms
# 15 颗的等效密度；长音不再只在字头喷一次）。
FLAME_PUFF_MS = 280
FLAME_MAX_PUFFS = 8
# 火焰出入场窗口（2026-10-18 用户口径）：发射总长跟随「入场/退场动画
# 时长」；文字全部出现/消失后再保留一小段（260ms）让火快速熄灭，不再
# 固定喷 ~2s 的长尾。数量随窗口同比放大（600ms = 32 颗基准）。
FLAME_TAIL_MS = 260
FLAME_MIN_COUNT = 8
FLAME_MAX_COUNT = 96
# 背光（flame_glow）：字后大尺寸低透明度火形，营造灼烧辉光；画在文字
# 之下（front=False），不叠亮文字本体。
FLAME_GLOW_LIFE_MS = 900
FLAME_GLOW_SIZE_SCALE = 2.80
# 逐字时间偏移表（2026-10-18 用户口径：不要每次哈希生成固定 ±80，而是
# 固化一组 20 个 [-80,80] 随机整数；所有逐字粒子特效按字符序循环取用）。
# 只偏移 burst 起点、不改寿命；表长 20，第 20 个字回到第 0 个偏移。
CHAR_TIME_OFFSETS_MS = (
    -29, 4, -64, 37, 48,
    59, -51, -24, -66, 78,
    -11, -15, 67, -33, 58,
    50, -68, -19, -48, -52,
)
CHAR_TIME_OFFSET_MAX_MS = max(CHAR_TIME_OFFSETS_MS)
# 余烬（ember）：小圆点火星，快速上蹿后受“重力”减速、带横向漂移与
# 闪烁；与灰烬（下坠的 ash）一升一落。
EMBER_A = "#FFD873"
EMBER_B = "#FF7A1A"
EMBER_LIFE_MS = 1300
# 灰烬（火焰烧尽的伴生粒子，kind ``ash`` 仅由 flame 退场发射，非独立档）：
# 灰白 + 炭黑双色；寿命短于火焰（余烬飘落即熄）。
ASH_A = "#9C9C9C"
ASH_B = "#555555"
ASH_LIFE_MS = 950


def _default_color_pair(
    snow: bool,
    heart: bool = False,
    fire: bool = False,
    ash: bool = False,
    feather: bool = False,
    lightning: bool = False,
) -> tuple[str, str]:
    """按 kind 的默认双色（固定默认档与「默认颜色」模式共用）：雪花=雪白
    +冰蓝；爱心=正红+浅粉；火焰=v3 火色阶两端（红边+白热芯）；灰烬=
    灰白+炭黑；羽毛=纯白+暖米白；闪电=冷白青+电光蓝；花瓣与其余 kind
    维持樱花粉（b5a1b7fb 用户口径不回归）。"""

    if ash:
        return (ASH_A, ASH_B)
    if fire:
        return (FIRE_A, FIRE_B)
    if heart:
        return (HEART_A, HEART_B)
    if feather:
        return (FEATHER_A, FEATHER_B)
    if lightning:
        return (LIGHTNING_A, LIGHTNING_B)
    return (SNOW_A, SNOW_B) if snow else (SAKURA_PINK_A, SAKURA_PINK_B)
# 出入场动画驱动的粒子（星光/涟漪/音符/花瓣/拼接/消散）默认用固定默认档：
# 樱花粉双色随机混发 + 40% 字号（2026-10 用户口径：出入场粒子用「默认
# 颜色」的樱花色，而不是单独颜色的默认白）；开启 ``fx_apply_to_entry_exit``
# 后颜色与尺寸改吃粒子旋钮（数量仍固定）。
ANIM_PARTICLE_SIZE_EM = 0.40
ANIM_PARTICLE_COUNT = 14
# 火焰出入场基准档（v3）：600ms 窗口每字 32 颗（随动画时长同比放大，
# 上限 FLAME_MAX_COUNT）；簇数/簇间隔也按窗口计算。不受粒子数量旋钮
# 影响（与其余出入场固定档同口径），背光/余烬为伴生层不占该预算。
ANIM_FLAME_COUNT = 32
# 入退场星光（逐字错峰）的整行扫过时长：逐字符 burst 按字符序从一端
# 排到另一端（入场左→右、退场反向），总时长按此值随编排时长等比缩放。
SPARKLE_SWEEP_MS = 350
# 星光族 sweep=0 的字内随机出生延迟上限与最短可见寿命：随机延迟在求值器
# 里按「寿命 - 最短可见寿命」钳制（短退场窗口不再吞掉整组粒子），规划期
# 错峰也按两者给最晚启动的字符预留时间（见 _sparkle_char_bursts）。改动
# 需与 C++ 镜像（d2d_fx.cpp sparkle 分支 sweep=0）同步。
SPARKLE_BIRTH_JITTER_MS = 130.0
SPARKLE_MIN_VISIBLE_MS = 40.0
# 星光族（sparkle/twinkle）纵向锚点分布：以锚点中心为原点的行高比例。
# 上缘 -0.62（字形顶 = -0.5，只稍微溢出字上方 ≈12% 行高）、下缘 +0.30
#（都在字框内）；线性映射 → 约 2/3 概率在字上侧（2026-10 用户口径：
# 尽量多出现在主文字上侧）。u < STAR_Y_SPLIT_U 落上侧。
STAR_Y_TOP = -0.62
STAR_Y_SPAN = 0.92
STAR_Y_SPLIT_U = 0.62 / 0.92
# 连续星星禁止同象限：哈希通道 6/7 重采样仍撞象限时按上一颗的竖侧
# 强制换侧（上半段/下半段各留 ~1/3 余量，仍落在偏置分布内）。


def star_y_fraction(u: float) -> float:
    """u∈[0,1] → 纵向锚点比例 ∈ [STAR_Y_TOP, STAR_Y_TOP+SPAN]。"""

    return STAR_Y_TOP + STAR_Y_SPAN * u


def _star_quadrant(left: bool, top: bool) -> int:
    return (2 if left else 0) + (1 if top else 0)


def star_y_resample(u2: float, alt6: float, alt7: float, prev_q: int, left: bool) -> float:
    """象限去重：与上一颗同象限时重采样 y（镜像 C++ fxStarYResample）。"""

    if _star_quadrant(left, star_y_fraction(u2) < 0.0) != prev_q:
        return u2
    if _star_quadrant(left, star_y_fraction(alt6) < 0.0) != prev_q:
        return alt6
    if _star_quadrant(left, star_y_fraction(alt7) < 0.0) != prev_q:
        return alt7
    if prev_q & 1:  # 上一颗在上 → 取下半段
        return STAR_Y_SPLIT_U + (1.0 - STAR_Y_SPLIT_U) * alt6
    # 上一颗在下 → 取上半段
    return STAR_Y_SPLIT_U * alt7


# ---------------------------------------------------------------------------
# 颜色模式解析：仿扫字线的 color / follow_before / follow_after / role
# ---------------------------------------------------------------------------

def _parse_hex_color(value: str) -> tuple[int, int, int] | None:
    """Parse ``#RRGGBB`` / ``#AARRGGBB`` into RGB (alpha dropped)."""

    text = str(value or "").strip()
    if not text.startswith("#") or len(text) not in {7, 9}:
        return None
    digits = text[1:]
    if any(character not in "0123456789abcdefABCDEF" for character in digits):
        return None
    if len(digits) == 8:
        digits = digits[2:]
    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def _average_hex_color(colors: list[str], fallback: str) -> str:
    """平均若干 #RRGGBB 得到一个实色（逐通道算术平均，非法色跳过）。"""

    parsed = [color for color in (_parse_hex_color(c) for c in colors) if color]
    if not parsed:
        return fallback
    red = sum(channel[0] for channel in parsed) // len(parsed)
    green = sum(channel[1] for channel in parsed) // len(parsed)
    blue = sum(channel[2] for channel in parsed) // len(parsed)
    return f"#{red:02X}{green:02X}{blue:02X}"


def fill_to_solid_color(fill: PaintFill, fallback: str) -> str:
    """把一份 PaintFill 折算成粒子可用的实色（#RRGGBB）。

    solid 直接取色；渐变/拼色取全部停止色的平均（sprite 是小尺寸实心
    剪影，平均色最接近整体观感）；图片填充取不到代表色，回退 fallback
    （图片走 ``paint`` 装饰规格的位图笔刷，实色仅作缺图回退）。
    """

    if fill.mode == "image":
        return fallback
    if fill.mode == "gradient_horizontal" or fill.mode == "gradient_vertical":
        stops = [color for _position, color in (fill.gradient_stops or [])]
        if not stops:
            stops = [fill.start_color, fill.end_color]
        return _average_hex_color(stops, fallback)
    if fill.mode == "split_vertical":
        stops = [color for _position, color in (fill.split_stops or [])]
        if not stops:
            stops = [fill.split_top_color, fill.split_bottom_color]
        return _average_hex_color(stops, fallback)
    return _average_hex_color([fill.color], fallback)


def _particle_color_state(
    style: Style,
    char_style: Style | None,
    mode: str,
) -> tuple[Style, KaraokeColorState] | None:
    """解析 (装饰来源样式, 走字前/后配色态)；``color`` 档与悬空引用为
    ``None``（调用方回退单独颜色）。

    ``follow_before`` / ``follow_after`` 用**当前字符**自己的角色方案
    （``char_style``，由调用方按 ``role_label`` 解析；整行锚点或未提供时
    回落行样式——歌手行的行样式已合并该歌手方案）；``role`` 用
    ``fx_particle_role_name`` 指定来源的走字后态（扫字线同口径）。
    """

    if mode == "follow_before":
        source = char_style if char_style is not None else style
        return source, effective_karaoke_colors(source).before
    if mode == "follow_after":
        source = char_style if char_style is not None else style
        return source, effective_karaoke_colors(source).after
    if mode == "role":
        resolved = appearance_role_source(
            style, getattr(style, "fx_particle_role_name", None)
        )
        if resolved is None:
            return None
        return resolved, effective_karaoke_colors(resolved).after
    return None


def particle_solid_color(style: Style, char_style: Style | None = None) -> str:
    """按 ``fx_particle_color_mode`` 解析粒子实色（行级口径，纯函数）。

    ``follow_before`` / ``follow_after`` 取**当前字符**角色方案（缺省回落
    行样式）有效配色的走字前/后「主文字」填充；``role`` 取
    ``fx_particle_role_name`` 指定来源（扫字线「复用配色方案」同口径）
    的「走字后-主文字」填充；填充折算见 :func:`fill_to_solid_color`。
    悬空引用 / 未知模式回退 ``color`` 档的 ``fx_particle_color``。
    """

    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    resolved = _particle_color_state(style, char_style, mode)
    if resolved is None:
        return fallback
    _source, state = resolved
    return fill_to_solid_color(state.text, fallback)


# 取色层级（fx_particle_color_layers）：从来源配色态裁剪装饰层。
# solid=仅实色 / stroke=+描边（按方案的描边栈，不加装饰）/
# decor=+装饰（仅装饰层，不加描边）/ all=全有（描边栈 + 装饰层，完全按方案）。
PARTICLE_COLOR_LAYERS = ("solid", "stroke", "decor", "all")


def _state_paint_spec(
    source: Style,
    state: KaraokeColorState,
    size_px: float,
    *,
    layers: str,
    after: bool,
    include_strokes: bool,
    fallback: str,
) -> dict[str, object] | None:
    """把一个配色态按**取色层级**折成 burst 级装饰规格（IR dict）。

    ``layers``（``fx_particle_color_layers``，2026-10 用户口径）：

    - ``solid`` 仅实色——**只有主文字填充层**：无描边、无描边2、无装饰；
      填充本身是渐变/图片时照样透传（2026-10-18 用户口径：仅实色 =
      “仅文字配色”，不是退化成单一实色）。纯 ``solid`` 填充才返回
      ``None`` 走纯色剪影；
    - ``stroke`` +描边——**按角色方案的描边栈**（方案启用二重描边时一起
      加），**不加装饰**；
    - ``decor`` +装饰——**不加描边**、仅加装饰层；
    - ``all`` 全有——描边栈 + 装饰层，完全按角色方案。

    装饰层**不固定阴影**：按来源角色方案的 ``decoration_kind`` 二选一——

    - ``shadow``：行空间常量偏移的剪影（``offset_x/y_px`` 按 粒子尺寸/
      来源字号 同比缩放），与文字 ``paint_shadow_silhouette`` 同口径；
    - ``glow``：多级描边近似高斯弥散晕（``radius_px`` 同口径缩放 +
      ``concentration_level``，``after`` 选走字前/后半径），与 glow 转场
      的既有双端配方（``GLOW_HALO_STROKES``）同源；
    - ``none``：装饰层不产出（``decor`` 档退化为纯色，``all`` 档退化为
      +描边）。

    装饰色取该态的 ``shadow`` 槽（文字发光/阴影同用此槽）。描边宽度按
    粒子尺寸/该来源字号 同比缩放（上限半个粒子边长）。``include_strokes
    =False``（涟漪光环）描边/二重描边/装饰全部不取——环体是发丝线，叠
    装饰会显著变粗（2026-10 用户口径）。图片填充原样透传：两条后端都把
    sprite 剪影按位图笔刷平铺绘制（锚点均为粒子中心的 em 空间原点，
    旋转变换与粒子复合，两端同口径），``color`` 实色仅作缺图回退；
    因此**来源主文字填充是渐变/图片模式时，即使取色层级为 ``solid``
    也产出规格**（仅实色档没有可用的纯色，渐变/图片本身就是「实色」层；
    2026-10-18 修复：此前只放行 image，gradient 被错误退化成平均实色）。
    """

    if layers not in PARTICLE_COLOR_LAYERS:
        return None
    if layers == "solid" and state.text.mode == "solid":
        return None

    def _layer_fill(fill: PaintFill) -> dict[str, object]:
        return paint_fill_to_dict(fill)

    want_strokes = include_strokes and layers in {"stroke", "all"}
    want_decor = include_strokes and layers in {"decor", "all"}
    scale = float(size_px) / max(float(source.font_size_px or 0.0), 1.0)
    half = max(float(size_px) / 2.0, 1.0)
    if want_strokes:
        stroke_width = min(
            max(float(source.stroke_width_px or 0) * scale, 0.0), half
        )
    else:
        stroke_width = 0.0
    if want_strokes:
        stroke2_raw = (
            max(int(source.stroke2_width_px or 0), 0)
            if source.stroke2_enabled
            else 0
        )
        stroke2_width = min(max(stroke2_raw * scale, 0.0), half)
    else:
        stroke2_width = 0.0
    spec: dict[str, object] = {
        "fill": _layer_fill(state.text),
        "stroke": _layer_fill(state.stroke),
        "stroke2": _layer_fill(state.stroke2),
        "stroke_width_px": round(stroke_width, 3),
        "stroke2_width_px": round(stroke2_width, 3),
    }
    if want_decor:
        # 装饰层随来源角色方案的 decoration_kind 二选一（2026-10 用户口径：
        # 不是固定阴影）——shadow=偏移剪影；glow=多级描边弥散晕（半径按
        # 走字前/后取，跟随尺寸同比缩放）；none=不产出。
        kind = decoration_kind_for_state(source, after=after)
        if kind in {"shadow", "glow"}:
            decor: dict[str, object] = {
                "kind": kind,
                "fill": _layer_fill(state.shadow),
            }
            if kind == "shadow":
                decor["offset_x_px"] = round(
                    float(shadow_offset_x(source, after=after)) * scale, 3
                )
                decor["offset_y_px"] = round(
                    float(shadow_offset_y(source, after=after)) * scale, 3
                )
            else:
                concentration = glow_concentration_level(source, after=after)
                radius = 0
                if concentration >= 0:
                    radius = int(
                        (
                            source.glow_after_radius_px
                            if after
                            else source.glow_before_radius_px
                        )
                        or 0
                    )
                decor["radius_px"] = round(max(radius, 0) * scale, 3)
                decor["concentration_level"] = concentration
            spec["decor"] = decor
    return spec


def particle_paint_spec(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    include_strokes: bool = True,
) -> dict[str, object] | None:
    """按颜色模式 + 取色层级解析**单态档**装饰规格（跟随前/后·复用方案）。

    ``color`` / ``sakura`` 档（无来源配色态）、悬空引用，以及
    ``fx_particle_color_layers == "solid"`` 且来源填充本身是纯 ``solid``
    时返回 ``None``——burst 只带实色 ``color``，走纯色剪影路径。仅实色
    档遇到渐变/图片填充仍产出**只含填充层**的规格（无描边/装饰，2026-10
    用户口径：“仅实色” = 仅文字配色，不是退化成平均实色）。双态档
    （前后各一 / 花瓣复用）的规格见 :func:`particle_variant_paints`。
    """

    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    resolved = _particle_color_state(style, char_style, mode)
    if resolved is None:
        return None
    source, state = resolved
    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    layers = str(
        getattr(style, "fx_particle_color_layers", "solid") or "solid"
    )
    return _state_paint_spec(
        source,
        state,
        size_px,
        layers=layers,
        # 走字前/后态：follow_after 与 role（走字后来源）用后侧发光半径。
        after=mode != "follow_before",
        include_strokes=include_strokes,
        fallback=fallback,
    )


def particle_variant_paints(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    petal: bool = False,
    snow: bool = False,
    heart: bool = False,
    fire: bool = False,
    ash: bool = False,
    feather: bool = False,
    lightning: bool = False,
    include_strokes: bool = True,
) -> list[dict[str, object]] | None:
    """双色随机模式的**变体实色/规格列表**（2026-10 花瓣特效口径）：

    - ``color`` 单独颜色——颜色一/颜色二双色槽随机混发（颜色二默认白色，
      白色即颜色本身——2026-10 用户口径：必须设置双色，无「未设置」态）；
    - ``sakura`` 默认颜色——按 kind 的默认双色变体（花瓣=樱花粉、
      雪花=雪白+冰蓝，见 :func:`_default_color_pair`）；
    - ``follow_mix`` 跟随字体·前后各一——当前字符角色方案（缺省回落行
      样式）配色的走字前/后两个变体；装饰层按 ``fx_particle_color_layers``
      取色层级裁剪（默认仅实色，见 :func:`_state_paint_spec`）；
    - ``role`` 复用配色方案——**仅花瓣粒子**取指定来源方案走字前/后两个
      变体（其余粒子 kind 的 ``role`` 保持单态「走字后」路径）。

    其余模式（follow_before / follow_after / 悬空 role）返回 ``None``
    （调用方走单态 + 取色层级路径）。「随机混发」由
    :func:`plan_line_bursts` 拆成两条同轨迹、不同种子/半数量的 burst 实现
    ——每颗粒子属且属一条变体，与逐粒子取色分布等价，渲染端零改动。
    """

    fallback = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
    mode = str(getattr(style, "fx_particle_color_mode", "color") or "color")
    if mode == "color":
        # 单独颜色：恒双色（颜色二默认白色——白色即颜色，无「未设置」
        # 态，2026-10 用户口径：必须设置双色）。两槽同色时折单 burst
        #（观感等价，默认双白不翻倍 burst）。
        color2 = str(getattr(style, "fx_particle_color2", "") or "#FFFFFF")
        if color2 == fallback:
            return [{"color": fallback}]
        return [{"color": fallback}, {"color": color2}]
    if mode == "sakura":
        pair = _default_color_pair(
            snow, heart, fire, ash, feather, lightning
        )
        return [{"color": pair[0]}, {"color": pair[1]}]

    layers = str(
        getattr(style, "fx_particle_color_layers", "solid") or "solid"
    )

    def _state_variants(source: Style) -> list[dict[str, object]]:
        colors = effective_karaoke_colors(source)
        out: list[dict[str, object]] = []
        for state, after in ((colors.before, False), (colors.after, True)):
            entry: dict[str, object] = {
                "color": fill_to_solid_color(state.text, fallback)
            }
            spec = _state_paint_spec(
                source,
                state,
                size_px,
                layers=layers,
                after=after,
                include_strokes=include_strokes,
                fallback=fallback,
            )
            if spec is not None:
                entry["paint"] = spec
            out.append(entry)
        return out

    if mode == "follow_mix":
        source = char_style if char_style is not None else style
        return _state_variants(source)
    if mode == "role" and petal:
        resolved = appearance_role_source(
            style, getattr(style, "fx_particle_role_name", None)
        )
        if resolved is None:
            return None
        return _state_variants(resolved)
    return None


# ---------------------------------------------------------------------------
# sprite 轮廓常量（±500 em 空间、中心原点；even-odd 填充口径）
# ---------------------------------------------------------------------------

def _circle_commands(
    cx: float, cy: float, radius: float, *, flip: bool = False
) -> list[list[object]]:
    """以 4 段三次贝塞尔近似圆（kappa=0.552284），返回 M/C/Z 命令列表。

    ``flip`` 反转绕向：光环 sprite 的内圆与外圆反向，使轮廓在 nonzero
    winding（D2D ``vectorGlyphGeometry`` 固定 WINDING）与 even-odd
    （QPainterPath 默认）两种填充规则下都呈圆环。
    """
    sign = -1.0 if flip else 1.0
    kappa = 0.552284
    commands: list[list[object]] = [["M", cx + radius, cy]]
    for k in range(4):
        a0 = k * (math.pi / 2.0) * sign
        a1 = (k + 1) * (math.pi / 2.0) * sign
        p0 = (cx + radius * math.cos(a0), cy + radius * math.sin(a0))
        p1 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1))
        # 切向随绕向同步翻转（sign）：反向绕行时控制点必须也换到行进方向
        # 另一侧，否则四段贝塞尔外翻、孔洞被挤成菱形。
        t0 = (-math.sin(a0) * sign, math.cos(a0) * sign)
        t1 = (-math.sin(a1) * sign, math.cos(a1) * sign)
        commands.append([
            "C",
            p0[0] + radius * kappa * t0[0], p0[1] + radius * kappa * t0[1],
            p1[0] - radius * kappa * t1[0], p1[1] - radius * kappa * t1[1],
            p1[0], p1[1],
        ])
    commands.append(["Z"])
    return commands


def _star4_commands() -> list[list[object]]:
    """四角星光：外径 500、内控点 0.11，四段二次贝塞尔凹边。"""
    inner = 55.0
    return [
        ["M", 0.0, -500.0],
        ["Q", inner, -inner, 500.0, 0.0],
        ["Q", inner, inner, 0.0, 500.0],
        ["Q", -inner, inner, -500.0, 0.0],
        ["Q", -inner, -inner, 0.0, -500.0],
        ["Z"],
    ]


def _note_commands() -> list[list[object]]:
    """八分音符剪影（单轮廓，无自交叠；装饰用途，28px 级别足够可读）。"""
    return [
        ["M", -260.0, 300.0],
        # 音头下半椭圆（中心 -90,300，rx 170 ry 120）
        ["C", -260.0, 366.0, -178.0, 420.0, -90.0, 420.0],
        ["C", -2.0, 420.0, 80.0, 366.0, 80.0, 300.0],
        # 符干右缘上行
        ["L", 80.0, -380.0],
        # 符尾外弧
        ["C", 200.0, -330.0, 260.0, -180.0, 205.0, -30.0],
        ["L", 135.0, -110.0],
        ["C", 130.0, -220.0, 110.0, -290.0, 30.0, -330.0],
        # 符干左缘下行
        ["L", 30.0, 300.0],
        # 音头上半椭圆
        ["C", 30.0, 234.0, -2.0, 180.0, -90.0, 180.0],
        ["C", -178.0, 180.0, -260.0, 234.0, -260.0, 300.0],
        ["Z"],
    ]


def _ring_commands() -> list[list[object]]:
    """水波纹环：外径 490 / 内径 462 的细描边圆环（厚约 2.8% em，60px 级
    直径下 ≈1.7px 发丝线）；内圆反向绕行（见 _circle_commands）。"""
    return (
        _circle_commands(0.0, 0.0, 490.0)
        + _circle_commands(0.0, 0.0, 462.0, flip=True)
    )


def _sprite_ir(commands: list[list[object]]) -> dict[str, object]:
    return {
        "path_commands": [list(command) for command in commands],
        "units_per_em": 1000,
        "advance_width": 1000,
    }


def _pixel_commands() -> list[list[object]]:
    """像素风方块：±180 em 方形（0.36em 边长），拼接/消散粒子的形体。"""
    return [
        ["M", -180.0, -180.0],
        ["L", 180.0, -180.0],
        ["L", 180.0, 180.0],
        ["L", -180.0, 180.0],
        ["Z"],
    ]


def _petal_commands() -> list[list[object]]:
    """樱花单瓣剪影（画法参考：圆润风筝形/泪滴形——底部收窄成尖、向上
    展宽，最宽处约 2/3 高度，顶端带 V 形缺刻（樱花区别于桃李杏梅的关键
    特征）；瓣长 ≈ 1.45 倍瓣宽，2026-10 用户口径：初版过宽）。单轮廓无
    自交叠（宽 0.48em × 高 0.70em）。"""
    return [
        ["M", 0.0, 370.0],
        # 左缘自底尖收窄处展宽上行
        ["C", -65.0, 335.0, -190.0, 175.0, -238.0, -25.0],
        # 左顶瓣绕向中线（最宽 ≈ ±242 在上部 2/3 处）
        ["C", -242.0, -225.0, -150.0, -355.0, -52.0, -295.0],
        # 顶端缺刻：左沿下沉到谷底再升起（缺刻深 ≈ 10% 瓣长）
        ["C", -26.0, -278.0, -11.0, -268.0, 0.0, -262.0],
        ["C", 11.0, -268.0, 26.0, -278.0, 52.0, -295.0],
        # 右顶瓣 + 右缘收窄下行回底尖
        ["C", 150.0, -355.0, 242.0, -225.0, 238.0, -25.0],
        ["C", 190.0, 175.0, 65.0, 335.0, 0.0, 370.0],
        ["Z"],
    ]


def _polygon_signed_area(points: list[tuple[float, float]]) -> float:
    """鞋带公式：>0 为逆时针（CCW）。"""
    total = 0.0
    count = len(points)
    for i in range(count):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % count]
        total += x0 * y1 - x1 * y0
    return total * 0.5


def _point_inside_polygon(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> bool:
    """射线交叉计数：点是否在（可能凹的）简单多边形内部。"""
    x, y = point
    inside = False
    count = len(polygon)
    for i in range(count):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % count]
        if (y0 > y) != (y1 > y):
            if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
    return inside


def _segment_crossing(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> tuple[float, float] | None:
    """线段 ab 与 cd 的交点参数 (t, u)（截断到 [0,1]）；平行/不相交为 None。"""
    r_x, r_y = b[0] - a[0], b[1] - a[1]
    s_x, s_y = d[0] - c[0], d[1] - c[1]
    den = r_x * s_y - r_y * s_x
    if abs(den) < 1e-12:
        return None
    q_x, q_y = c[0] - a[0], c[1] - a[1]
    t = (q_x * s_y - q_y * s_x) / den
    u = (q_x * r_y - q_y * r_x) / den
    limit = 1e-9
    if -limit <= t <= 1.0 + limit and -limit <= u <= 1.0 + limit:
        return (min(max(t, 0.0), 1.0), min(max(u, 0.0), 1.0))
    return None


def _union_outline(
    polygons: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """简单多边形并集的边界环（外环 + 洞环，全部保持 CCW 元素边的绕向）。

    为什么不是「多个子路径直接画重叠元素」：Python painter 用 even-odd
    填充、C++ D2D 后端用 winding 填充（painter.py / d2d_geometry_resources.cpp），
    重叠或嵌套的同向子路径在两种规则下结果不同。改取精确并集轮廓后，
    「外环 + 反向洞环」（ring sprite 同款约定）两规则渲染一致。
    元素需自身简单、两两无共线重边、无三线共点（雪花元素按参数构造满足）；
    串联不成环时直接抛错——坏 sprite 不允许静默进入产品。"""
    normalized: list[list[tuple[float, float]]] = []
    for polygon in polygons:
        points = [(float(x), float(y)) for x, y in polygon]
        if _polygon_signed_area(points) < 0.0:
            points.reverse()
        normalized.append(points)
    edges: list[tuple[int, tuple[float, float], tuple[float, float]]] = []
    for index, polygon in enumerate(normalized):
        count = len(polygon)
        for i in range(count):
            edges.append((index, polygon[i], polygon[(i + 1) % count]))
    cuts: list[list[float]] = [[0.0, 1.0] for _ in edges]
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if edges[i][0] == edges[j][0]:
                continue  # 自身简单：同元素边不互切
            crossing = _segment_crossing(
                edges[i][1], edges[i][2], edges[j][1], edges[j][2]
            )
            if crossing is not None:
                cuts[i].append(crossing[0])
                cuts[j].append(crossing[1])
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for i, (owner, start, end) in enumerate(edges):
        # 参数不四舍五入：截断参数的重建误差会放大到 ~1e-7，跨过串联
        # 端点键（round 6 位）的分档边界；零长段由下一行的阈值兜底。
        stops = sorted(set(cuts[i]))
        for t0, t1 in zip(stops, stops[1:]):
            if t1 - t0 <= 1e-9:
                continue
            p0 = (start[0] + (end[0] - start[0]) * t0, start[1] + (end[1] - start[1]) * t0)
            p1 = (start[0] + (end[0] - start[0]) * t1, start[1] + (end[1] - start[1]) * t1)
            middle = ((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5)
            for k, polygon in enumerate(normalized):
                if k != owner and _point_inside_polygon(middle, polygon):
                    break
            else:
                segments.append((p0, p1))

    def _key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 6), round(point[1], 6))

    starting: dict[tuple[float, float], list[int]] = {}
    for index, (start, _end) in enumerate(segments):
        starting.setdefault(_key(start), []).append(index)
    used = [False] * len(segments)
    loops: list[list[tuple[float, float]]] = []
    for first in range(len(segments)):
        if used[first]:
            continue
        loop: list[tuple[float, float]] = []
        current = first
        start_key = _key(segments[first][0])
        while True:
            used[current] = True
            loop.append(segments[current][0])
            following = None
            for candidate in starting.get(_key(segments[current][1]), ()):
                if not used[candidate]:
                    following = candidate
                    break
            if following is None or _key(segments[following][0]) == start_key:
                break
            current = following
        if _key(segments[current][1]) != start_key or len(loop) < 3:
            raise ValueError("多边形并集边界串联失败（元素几何退化）")
        loops.append(loop)
    return loops


def _snow_element_polygons() -> list[list[tuple[float, float]]]:
    """雪花元素多边形（教程结构）：中心六边形 + 六主臂 + 每臂两条内层触接枝
    + 每臂两条外层自由枝。供 `_union_outline` 取并集轮廓（测试亦直接引用）。"""
    tip_r = 470.0  # 臂尖半径（包络 ≤ 500）
    arm_half = 30.0  # 臂半宽
    arm_base = 96.0  # 臂基部半径（落在六边形环的环壁内）
    hex_r = 119.0  # 中心六边形外沿顶点半径
    hex_inner_r = 93.5  # 中心六边形内沿顶点半径（环壁厚 ≈ 22）
    hex_overlap = 8.0  # 六边形环相邻块的沿边搭接量（避免共线重边）
    anchor_d = 182.0  # 内层枝锚点沿臂距离（落在臂缘上）
    branch_half = 13.0  # 枝半宽
    outer_d = 330.0  # 外层枝锚点沿臂距离
    sink = 30.0  # 枝基部没入臂内的长度
    taper_ratio = math.sqrt(3.0)  # 60° 尖：沿轴收束长度 = 半宽 × √3

    arm_taper = arm_half * taper_ratio
    branch_taper = branch_half * taper_ratio
    star_arm_r = anchor_d - arm_half / math.sqrt(3.0)  # 星环中线在臂轴上的交线半径
    star_tip_r = star_arm_r * math.sqrt(3.0)  # 相邻两枝轴线交点半径（触接处）
    branch_len = anchor_d - arm_half * taper_ratio  # 臂缘锚点 → 触接交点的轴线长

    polygons: list[list[tuple[float, float]]] = []
    # 中心六边形**环**（教程里六边形是空心轮廓；实心剪影下拆成六块沿边搭接
    # 的平行四边形，中心自然留出六边形白洞）。
    for k in range(6):
        start_angle = k * math.pi / 3.0
        end_angle = start_angle + math.pi / 3.0
        edge_angle = start_angle + 2.0 * math.pi / 3.0  # 该边方向（外沿起点 → 终点）
        ex, ey = math.cos(edge_angle), math.sin(edge_angle)
        outer_start = (hex_r * math.cos(start_angle), hex_r * math.sin(start_angle))
        outer_end = (hex_r * math.cos(end_angle), hex_r * math.sin(end_angle))
        inner_start = (
            hex_inner_r * math.cos(start_angle),
            hex_inner_r * math.sin(start_angle),
        )
        inner_end = (hex_inner_r * math.cos(end_angle), hex_inner_r * math.sin(end_angle))
        polygons.append(
            [
                (outer_start[0] - hex_overlap * ex, outer_start[1] - hex_overlap * ey),
                (outer_end[0] + hex_overlap * ex, outer_end[1] + hex_overlap * ey),
                (inner_end[0] + hex_overlap * ex, inner_end[1] + hex_overlap * ey),
                (inner_start[0] - hex_overlap * ex, inner_start[1] - hex_overlap * ey),
            ]
        )
    # 主臂：60° 尖的细长五边形。
    for k in range(6):
        angle = k * math.pi / 3.0
        ux, uy = math.cos(angle), math.sin(angle)
        nx, ny = -uy, ux
        polygons.append(
            [
                (arm_base * ux + arm_half * nx, arm_base * uy + arm_half * ny),
                (
                    (tip_r - arm_taper) * ux + arm_half * nx,
                    (tip_r - arm_taper) * uy + arm_half * ny,
                ),
                (tip_r * ux, tip_r * uy),
                (
                    (tip_r - arm_taper) * ux - arm_half * nx,
                    (tip_r - arm_taper) * uy - arm_half * ny,
                ),
                (arm_base * ux - arm_half * nx, arm_base * uy - arm_half * ny),
            ]
        )
    # 细枝：side=+1 指向下一臂、side=-1 指向上一条臂（皆与主臂成 60°）。
    # 外层枝自由 60° 尖；内层枝远端由臂间中线上的星尖 M 斜切封口，两名
    # 相邻内层枝的外缘即在 M 相交成 60° 星尖（并集自动成 miter）。
    for k in range(6):
        angle = k * math.pi / 3.0
        ux, uy = math.cos(angle), math.sin(angle)
        nx, ny = -uy, ux
        for side in (1.0, -1.0):
            bisector = angle + side * math.pi / 6.0
            for anchor_radius, cut_tip in ((anchor_d, True), (outer_d, False)):
                start = (
                    anchor_radius * ux + side * arm_half * nx,
                    anchor_radius * uy + side * arm_half * ny,
                )
                direction = angle + side * math.pi / 3.0
                dx, dy = math.cos(direction), math.sin(direction)
                lx, ly = -dy, dx  # 枝局部左法向
                outer = -side  # 外缘在“朝臂间中线”一侧
                inner_base = (
                    start[0] - sink * dx - outer * branch_half * lx,
                    start[1] - sink * dy - outer * branch_half * ly,
                )
                outer_base = (
                    start[0] - sink * dx + outer * branch_half * lx,
                    start[1] - sink * dy + outer * branch_half * ly,
                )
                if cut_tip:
                    tip_radius = star_tip_r + 2.0 * branch_half
                    apex = (
                        tip_radius * math.cos(bisector),
                        tip_radius * math.sin(bisector),
                    )
                    inner_end = (
                        start[0] + branch_len * dx - outer * branch_half * lx,
                        start[1] + branch_len * dy - outer * branch_half * ly,
                    )
                    polygons.append([outer_base, apex, inner_end, inner_base])
                else:
                    apex = (start[0] + branch_len * dx, start[1] + branch_len * dy)
                    shoulder = branch_len - branch_taper
                    outer_shoulder = (
                        start[0] + shoulder * dx + outer * branch_half * lx,
                        start[1] + shoulder * dy + outer * branch_half * ly,
                    )
                    inner_shoulder = (
                        start[0] + shoulder * dx - outer * branch_half * lx,
                        start[1] + shoulder * dy - outer * branch_half * ly,
                    )
                    polygons.append(
                        [outer_base, outer_shoulder, apex, inner_shoulder, inner_base]
                    )
    return polygons


def _loops_to_commands(loops: list[list[tuple[float, float]]]) -> list[list[object]]:
    """并集环 → path_commands（每环一个 M..Z 子路径）。"""
    commands: list[list[object]] = []
    for loop in loops:
        commands.append(["M", loop[0][0], loop[0][1]])
        for x, y in loop[1:]:
            commands.append(["L", x, y])
        commands.append(["Z"])
    return commands


def _snow_loops() -> list[list[tuple[float, float]]]:
    """雪花并集轮廓环：1 外环（CCW）+ 6 窗洞 + 1 中心六边形洞（CW）。"""
    loops = _union_outline(_snow_element_polygons())
    if len(loops) != 8:
        raise ValueError(
            f"雪花并集轮廓环数异常：{len(loops)}（期望 1 外环 + 6 窗洞 + 1 中心洞）"
        )
    return loops


def _snow_commands() -> list[list[object]]:
    """雪花剪影·镂空（kind ``snow``）：letsdrawthat 教程「六臂枝晶」画法
    （2026-10 用户口径，目标为教程成图而非线条星形）：**空心中心六边形**
    （顶点朝向六臂）+ 六条 60° 尖锐主臂 + **内层 12 条细枝两两触接**（端点在
    臂间 30° 中线上汇成六角星）+ 外层每臂一对同形细枝（近尖端，仿箭羽）。
    比例按教程成图取样：六边形外沿顶点半径 0.253R、星环交线半径臂上 0.35R
    （中线 0.607R）、臂宽 0.128R；枝宽为粒子尺寸下的可读性略加粗（0.028R）。
    元素多边形取精确并集轮廓：外环 + 六个「六边形与星环之间的窗洞」环 +
    中心六边形洞环——even-odd / winding 两填充规则渲染一致（见
    `_union_outline`）。"""
    return _loops_to_commands(_snow_loops())


def _snow_solid_commands() -> list[list[object]]:
    """雪花剪影·实心（kind ``snow_solid``）：**0890458d 时代的 ❄ 枝晶星形**
    （2026-10-07 用户口径指定保留——「实心」档不是镂空档去洞环，而是当时
    那版形状）。每 60° 扇区 = 主臂尖（r 470）+ 两侧枝尖（±20°，r 250）+
    臂间谷（r 85），共 24 顶点直线轮廓——主臂六重对称、侧枝朝外张开，
    粒子小尺寸下读作带枝晶的雪花；单轮廓无自交叠（M + 23×L + Z）。"""
    commands: list[list[object]] = []
    vertex = 0
    for arm in range(6):
        arm_angle = -math.pi / 2.0 + arm * math.pi / 3.0
        ring = (
            (arm_angle, 470.0),                      # 主臂尖
            (arm_angle + math.pi / 9.0, 250.0),      # 顺时针侧枝尖（+20°）
            (arm_angle + math.pi / 6.0, 85.0),       # 臂间谷（+30°）
            (arm_angle + math.pi * 2.0 / 9.0, 250.0),  # 逆时针侧枝尖（+40°）
        )
        for angle, radius in ring:
            commands.append([
                "M" if vertex == 0 else "L",
                radius * math.cos(angle),
                radius * math.sin(angle),
            ])
            vertex += 1
    commands.append(["Z"])
    return commands


def _flame_commands() -> list[list[object]]:
    """火焰主体剪影（kind ``flame``）——**圆钝火团**（2026-10-18 用户
    口径：Aeg 模板的火粒本体是“简单几何 + blur”的软团，不是锐利叶片；
    v3 第一版锐利 S 形火舌在小尺寸下仍读作树叶）。外两层用本形状：
    圆底、上收成短焰尖、略不对称（焰尖偏右），叠加 semi-transparent
    半影后形成连续火焰体积；内两层另用 ``flame_alt`` 细长焰芯提供
    火舌的锐利感。M + 6×C + Z 单轮廓，h/w ≈ 1.8。"""
    return [
        ["M", 20.0, -430.0],
        # 焰尖 → 左上缘（圆钝外扩）
        ["C", -5.0, -330.0, -150.0, -300.0, -205.0, -190.0],
        # 左腰 → 左底
        ["C", -255.0, -90.0, -235.0, 60.0, -170.0, 175.0],
        ["C", -110.0, 280.0, -45.0, 360.0, 0.0, 390.0],
        # 右底 → 右腰
        ["C", 45.0, 360.0, 110.0, 280.0, 170.0, 175.0],
        ["C", 235.0, 60.0, 255.0, -90.0, 205.0, -190.0],
        # 右上缘回尖
        ["C", 150.0, -300.0, 5.0, -330.0, 20.0, -430.0],
        ["Z"],
    ]


def _flame_alt_commands() -> list[list[object]]:
    """细长内焰剪影（kind ``flame_alt``，火焰 v3 内两层专用）：比主火舌
    更窄（±180）、更直，带轻微 S 摆——叠在宽火舌中心时读作被包住的
    高温焰芯，而不是同一张剪影的缩小副本。M + 8×C + Z 单轮廓。"""
    return [
        ["M", 30.0, -480.0],
        # 左上缘
        ["C", 10.0, -400.0, -80.0, -370.0, -120.0, -290.0],
        # 左腰内收
        ["C", -155.0, -220.0, -105.0, -160.0, -70.0, -100.0],
        # 左下段外鼓
        ["C", -140.0, -40.0, -180.0, 60.0, -150.0, 150.0],
        # 底部
        ["C", -120.0, 245.0, -60.0, 330.0, 0.0, 380.0],
        ["C", 60.0, 330.0, 120.0, 245.0, 150.0, 150.0],
        # 右下缘
        ["C", 180.0, 60.0, 160.0, -40.0, 110.0, -110.0],
        # 右上缘回尖
        ["C", 150.0, -200.0, 130.0, -340.0, 55.0, -420.0],
        ["C", 45.0, -440.0, 38.0, -462.0, 30.0, -480.0],
        ["Z"],
    ]


def _ash_commands() -> list[list[object]]:
    """灰烬 speck（kind ``ash``，flame 退场伴生）：不规则小碎块——五边
    微歪四边形，±150 em（≈0.15em 视觉），小尺寸下读作飘散的灰烬碎屑。"""
    return [
        ["M", -148.0, -96.0],
        ["L", 112.0, -142.0],
        ["L", 150.0, 58.0],
        ["L", 26.0, 148.0],
        ["L", -138.0, 104.0],
        ["Z"],
    ]


def _heart_commands() -> list[list[object]]:
    """爱心剪影（kind ``heart``，2026-10 用户口径：全面克隆音符飘出的
    三档特效，只换形状与默认红/粉双色）。形状取自 **Twemoji ❤️（U+2764，
    twemoji 15.1.0 assets/svg/2764.svg，图形 CC-BY 4.0）** 的官方 path——
    emoji 心形即「圆瓣浅缺刻 + 钝圆底」的软萌轮廓权威定义（2026-10-18
    用户口径：手绘经典收尖底「过于性感」，且明确要求联网取现成形状、
    不要自行设计）。坐标经「中心平移 + 等比缩放」归一到 ±500 em 空间
    （宽 ±470 / 高 ±421，h/w ≈ 0.90 略宽于高）；M + 8×C + Z 单轮廓。"""
    return [
        ["M", 470.0, -162.0],
        ["C", 470.0, -305.3, 353.9, -421.4, 210.7, -421.4],
        ["C", 123.8, -421.4, 47.1, -378.5, 0.0, -312.9],
        ["C", -47.1, -378.5, -123.8, -421.4, -210.7, -421.4],
        ["C", -353.9, -421.4, -470.0, -305.3, -470.0, -162.0],
        ["C", -470.0, -141.8, -467.4, -122.1, -463.0, -103.2],
        ["C", -427.0, 120.6, -178.3, 356.6, 0.0, 421.4],
        ["C", 178.3, 356.6, 427.0, 120.6, 463.0, -103.1],
        ["C", 467.4, -122.1, 470.0, -141.7, 470.0, -162.0],
        ["Z"],
    ]


def _feather_commands() -> list[list[object]]:
    """羽毛剪影（kind ``feather``，2026-10）：细长羽片 + 裸露羽轴。

    AE 羽毛参考里羽毛的可读性来自“羽片两侧的羽枝缺口 + 一根明显的裸
    羽轴”，不是光滑叶片。轮廓为单闭环：顶部收尖，右侧 5 组、左侧 4 组
    羽枝缺口（锯齿落在单调下降的边上，不自交），底部羽片收到 70 em 宽
    的羽轴根后拉出一根 180 em 长、收尖的裸羽轴；宽高比 ≈ 0.4，小尺寸
    （32px）下仍能读出羽毛而不是花瓣。M + 5×C + 20×L + Z。"""
    return [
        ["M", 0.0, -500.0],
        # 顶部 → 右上缘
        ["C", 55.0, -470.0, 125.0, -400.0, 168.0, -305.0],
        # 右缘羽枝缺口（向外突出 → 内收，交替下行）
        ["L", 126.0, -280.0],
        ["L", 184.0, -240.0],
        ["L", 140.0, -208.0],
        ["L", 198.0, -162.0],
        ["L", 146.0, -132.0],
        ["L", 202.0, -78.0],
        ["L", 146.0, -48.0],
        ["L", 190.0, 18.0],
        ["L", 118.0, 86.0],
        ["L", 154.0, 158.0],
        # 羽片右下缘 → 羽轴根
        ["C", 122.0, 218.0, 74.0, 268.0, 40.0, 305.0],
        ["L", 56.0, 330.0],
        ["L", 24.0, 510.0],
        ["L", -16.0, 330.0],
        ["L", -2.0, 305.0],
        # 左下缘 → 左缘羽枝缺口（上行）
        ["C", -66.0, 276.0, -130.0, 226.0, -174.0, 146.0],
        ["L", -132.0, 120.0],
        ["L", -186.0, 78.0],
        ["L", -142.0, 52.0],
        ["L", -190.0, 5.0],
        ["L", -146.0, -22.0],
        ["L", -198.0, -70.0],
        # 左上缘回到顶尖
        ["C", -230.0, -130.0, -240.0, -220.0, -216.0, -300.0],
        ["C", -190.0, -390.0, -115.0, -455.0, -30.0, -492.0],
        ["Z"],
    ]


def _segment_quad(
    start: tuple[float, float],
    end: tuple[float, float],
    half_start: float,
    half_end: float,
    *,
    extend: float = 0.0,
) -> list[tuple[float, float]]:
    """把一条有宽度的线段展成四边形（沿轴向两端各延长 ``extend``）。

    延长量让相邻线段在折点处重叠，:func:`_union_outline` 才能把它们
    并成一根连续的闪电主干（只在端点相触会造成边界串联失败）。
    """
    ax, ay = start
    bx, by = end
    length = math.hypot(bx - ax, by - ay) or 1.0
    ux, uy = (bx - ax) / length, (by - ay) / length
    nx, ny = -uy, ux
    return [
        (ax - ux * extend + nx * half_start, ay - uy * extend + ny * half_start),
        (bx + ux * extend + nx * half_end, by + uy * extend + ny * half_end),
        (bx + ux * extend - nx * half_end, by + uy * extend - ny * half_end),
        (ax - ux * extend - nx * half_start, ay - uy * extend - ny * half_start),
    ]


def _lightning_element_polygons() -> list[list[tuple[float, float]]]:
    """闪电 bolt 的宽度元素：锯齿主干 + 四条分叉，供并集轮廓使用。"""
    polygons: list[list[tuple[float, float]]] = []
    for index in range(len(_LIGHTNING_TRUNK) - 1):
        polygons.append(
            _segment_quad(
                _LIGHTNING_TRUNK[index],
                _LIGHTNING_TRUNK[index + 1],
                _LIGHTNING_HALF_WIDTHS[index],
                _LIGHTNING_HALF_WIDTHS[index + 1],
                extend=10.0,
            )
        )
    for start, end, half_start, half_end in _LIGHTNING_BRANCHES:
        polygons.append(
            _segment_quad(start, end, half_start, half_end, extend=6.0)
        )
    return polygons


def _lightning_commands() -> list[list[object]]:
    """闪电剪影（kind ``bolt``）：锯齿主干 + 左/右分叉的并集轮廓。

    并集保证「单外环、无洞」——QPainter even-odd 与 D2D winding 两种填充
    规则下形状一致（分叉单独画两条子路径在 even-odd 下重叠区会被挖空）。
    """
    loops = _union_outline(_lightning_element_polygons())
    if len(loops) != 1:
        raise ValueError(
            f"闪电并集轮廓环数异常：{len(loops)}（期望单外环）"
        )
    return _loops_to_commands(loops)


FX_SPRITES: dict[str, dict[str, object]] = {
    "star4": _sprite_ir(_star4_commands()),
    "ring": _sprite_ir(_ring_commands()),
    "note": _sprite_ir(_note_commands()),
    "pixel": _sprite_ir(_pixel_commands()),
    "petal": _sprite_ir(_petal_commands()),
    "feather": _sprite_ir(_feather_commands()),
    "bolt": _sprite_ir(_lightning_commands()),
    "snow": _sprite_ir(_snow_commands()),
    "snow_solid": _sprite_ir(_snow_solid_commands()),
    "heart": _sprite_ir(_heart_commands()),
    "flame": _sprite_ir(_flame_commands()),
    "flame_alt": _sprite_ir(_flame_alt_commands()),
    "ash": _sprite_ir(_ash_commands()),
    # 火星圆点（ember 专用）：半径 500 的实心圆，四段贝塞尔近似。
    "dot": _sprite_ir(_circle_commands(0.0, 0.0, 500.0)),
}

_SPRITE_FOR_KIND = {
    "sparkle": "star4",
    "ripple": "ring",
    "twinkle": "star4",
    "twinkle_classic": "star4",
    "note": "note",
    "assemble": "pixel",
    "dissolve": "pixel",
    "petal": "petal",
    "feather": "feather",
    "lightning": "bolt",
    "lightning_glow": "bolt",
    "snow": "snow",
    "snow_solid": "snow_solid",
    "heart": "heart",
    "flame": "flame",
    "flame_alt": "flame_alt",
    "flame_glow": "flame",
    "ember": "dot",
    "ash": "ash",
}


def sprite_for_kind(kind: str) -> str:
    return _SPRITE_FOR_KIND.get(kind, "star4")


# ---------------------------------------------------------------------------
# burst 规划：同一函数供渲染 IR（native/protocol）与 QPainter 调用
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 装饰规格缓存：同一「样式 × 角色方案 × 粒子尺寸」组合只解析一次，
# 之后所有 burst / 所有帧复用同一份规格 dict（含涟漪径向采样的输入）。
# 2026-10 用户口径：用过的粒子+角色组合只烘焙一次。强引用持有键里的
# Style，防 id() 复用串缓存；LRU 上限覆盖「多角色行 × 数档尺寸」。
# ---------------------------------------------------------------------------
_PAINT_CACHE_MAX = 64
_PAINT_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], dict[str, object] | None]]" = OrderedDict()
# 实色回退同理缓存（渐变平均色逐 burst 重算同样昂贵）。
_SOLID_CACHE_MAX = 64
_SOLID_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], str]]" = OrderedDict()
# 双色档（sakura / follow_mix / role+花瓣）的变体规格同样缓存
#（含每个态的完整装饰规格——渐变停止色/描边宽折算逐变体重算同样昂贵）。
_VARIANT_CACHE_MAX = 64
_VARIANT_CACHE: "OrderedDict[tuple, tuple[tuple[Style, Style | None], list[dict[str, object]] | None]]" = OrderedDict()


def _cached_particle_solid(style: Style, char_style: Style | None) -> str:
    key = (id(style), id(char_style) if char_style is not None else 0)
    entry = _SOLID_CACHE.get(key)
    if entry is not None and entry[0][0] is style and (
        entry[0][1] is char_style
        if char_style is not None
        else entry[0][1] is None
    ):
        _SOLID_CACHE.move_to_end(key)
        return entry[1]
    color = particle_solid_color(style, char_style)
    if len(_SOLID_CACHE) >= _SOLID_CACHE_MAX:
        _SOLID_CACHE.popitem(last=False)
    _SOLID_CACHE[key] = ((style, char_style), color)
    return color


def _cached_particle_paint(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    include_strokes: bool,
) -> dict[str, object] | None:
    key = (
        id(style),
        id(char_style) if char_style is not None else 0,
        round(float(size_px), 3),
        bool(include_strokes),
    )
    entry = _PAINT_CACHE.get(key)
    if entry is not None:
        # 键对象仍被条目强引用：id 未被复用，命中即安全。
        if entry[0][0] is style and (
            entry[0][1] is char_style
            if char_style is not None
            else entry[0][1] is None
        ):
            _PAINT_CACHE.move_to_end(key)
            return entry[1]
    spec = particle_paint_spec(
        style, char_style, size_px, include_strokes=include_strokes
    )
    if len(_PAINT_CACHE) >= _PAINT_CACHE_MAX:
        _PAINT_CACHE.popitem(last=False)
    _PAINT_CACHE[key] = ((style, char_style), spec)
    return spec


def _cached_particle_variants(
    style: Style,
    char_style: Style | None,
    size_px: float,
    *,
    petal: bool,
    snow: bool = False,
    heart: bool = False,
    fire: bool = False,
    ash: bool = False,
    feather: bool = False,
    lightning: bool = False,
    include_strokes: bool,
) -> list[dict[str, object]] | None:
    key = (
        id(style),
        id(char_style) if char_style is not None else 0,
        round(float(size_px), 3),
        bool(petal),
        bool(snow),
        bool(heart),
        bool(fire),
        bool(ash),
        bool(feather),
        bool(lightning),
        bool(include_strokes),
    )
    entry = _VARIANT_CACHE.get(key)
    if entry is not None and entry[0][0] is style and (
        entry[0][1] is char_style
        if char_style is not None
        else entry[0][1] is None
    ):
        _VARIANT_CACHE.move_to_end(key)
        return entry[1]
    variants = particle_variant_paints(
        style,
        char_style,
        size_px,
        petal=petal,
        snow=snow,
        heart=heart,
        fire=fire,
        ash=ash,
        feather=feather,
        lightning=lightning,
        include_strokes=include_strokes,
    )
    if len(_VARIANT_CACHE) >= _VARIANT_CACHE_MAX:
        _VARIANT_CACHE.popitem(last=False)
    _VARIANT_CACHE[key] = ((style, char_style), variants)
    return variants


def clear_particle_paint_cache() -> None:
    """清空装饰规格缓存（测试隔离用；常规渲染无需失效——键含完整签名）。"""

    _PAINT_CACHE.clear()
    _SOLID_CACHE.clear()
    _VARIANT_CACHE.clear()



def exit_particle_tail_end_ms(
    exit_anim: str,
    char_count: int,
    line_end_ms: int | None,
    display_end_ms: int,
) -> int | None:
    """退场粒子（标准速率）最后一只 burst 的自然终点（ms）。

    2026-10 分离口径：七个粒子档的退场发射恒按标准速率且完整播放，
    终点可以越过显示窗终点 ``display_end_ms``——渲染端的「退场尾奏
    通道」（行已消失、仅续播粒子）用它判定本行是否还有存活的粒子。
    非粒子退场档（含拼接/消散——粒子即文字形体，与时长耦合）返回
    ``None``。与 :func:`plan_line_bursts` 各退场分支同一算式。
    """
    line_end = int(line_end_ms) if line_end_ms is not None else 0
    if exit_anim == "sparkle":
        base = max(line_end, display_end_ms - SPARKLE_EXIT_LIFE_MS)
        steps = max(char_count - 1, 1)
        return (
            base
            + (SPARKLE_SWEEP_MS // steps) * steps
            + SPARKLE_EXIT_LIFE_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim == "ripple":
        base = max(line_end, display_end_ms - (RIPPLE_CHAR_STAGGER_MS + RIPPLE_LIFE_MS))
        return (
            base
            + RIPPLE_CHAR_STAGGER_MS
            + RIPPLE_LIFE_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim in ("note", "heart"):
        base = max(line_end, display_end_ms - (RIPPLE_CHAR_STAGGER_MS + SING_NOTE_TOTAL_MS))
        return (
            base
            + RIPPLE_CHAR_STAGGER_MS
            + SING_NOTE_TOTAL_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim == "flame":
        # 火焰烧尽（2026-10-18 用户口径）：火粒窗口跟随退场时长，文字
        # 全部消失后只保留 FLAME_TAIL_MS 短尾——尾奏界就是短尾终点。
        return int(display_end_ms) + FLAME_TAIL_MS
    if exit_anim == "feather":
        base = max(
            line_end,
            display_end_ms - (RIPPLE_CHAR_STAGGER_MS + FEATHER_LIFE_MS),
        )
        return (
            base
            + RIPPLE_CHAR_STAGGER_MS
            + FEATHER_LIFE_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim == "lightning":
        base = max(
            line_end,
            display_end_ms - (RIPPLE_CHAR_STAGGER_MS + LIGHTNING_WINDOW_MS),
        )
        return (
            base
            + RIPPLE_CHAR_STAGGER_MS
            + LIGHTNING_WINDOW_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim == "petal":
        base = max(line_end, display_end_ms - (RIPPLE_CHAR_STAGGER_MS + PETAL_LIFE_MS))
        return (
            base
            + RIPPLE_CHAR_STAGGER_MS
            + PETAL_LIFE_MS
            + CHAR_TIME_OFFSET_MAX_MS
        )
    if exit_anim in SNOW_KINDS:
        return display_end_ms + 300
    return None


def plan_line_bursts(
    style: Style,
    line_index: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    line_end_ms: int | None,
    char_windows: list[tuple[int, int]],
    *,
    char_visible: list[bool] | None = None,
    char_styles: list[Style | None] | None = None,
) -> list[dict[str, object]]:
    """把 style 的粒子档位规划成逐行 burst 列表（IR-ready dict）。

    时间锚与入退场动画同源：入场自显示窗口起点，退场自
    ``max(行末, 显示末-标准窗)`` 回溯。**粒子发射与文字出入场分离**
    （2026-10 用户口径）：七个粒子档（星光/涟漪/音符/爱心/花瓣/雪花
    两档）的发射**恒按标准速率**（错峰/寿命/窗口全部取标准常量，
    = 旧 600ms 基准的时序），且退场粒子**完整播放**——终点可越过
    显示窗终点（渲染端退场尾奏通道续播，见 :func:`exit_particle_tail_end_ms`）；
    「入场/退场动画时长」旋钮此后只控制文字逐字渐显/渐隐（拼接/
    消散的粒子即文字形体，维持与时长耦合）。唱字自各字符唱字窗起点
    发射、窗口延伸到轨迹自然播完（不受唱字窗约束）。
    尺寸语义：``fx_particle_size_em`` 为相对主字号的 em 比例（0.40 =
    40% 字号），此处按全局字号折算成像素下发，两后端同值。
    颜色语义：``color`` 恒为实色回退（旧 sidecar 兼容）；颜色模式非
    ``color`` 时另附 ``paint`` 完整装饰规格（填充/描边/二重描边 PaintFill
    + 已按粒子尺寸缩放的描边宽，见 :func:`particle_paint_spec`），两条
    后端优先按 ``paint`` 绘制；双色档（单独颜色双槽 / ``sakura`` /
    ``follow_mix`` 前后各一 / ``role``+花瓣）改拆两条实色变体 burst
    （数量对半、种子错开）。入退场动画粒子默认固定樱花粉双色档，开启
    ``fx_apply_to_entry_exit`` 后改吃粒子旋钮的颜色与尺寸（数量恒固定）。
    ``line_index`` 参与种子，保证同曲目每行轨迹不同且重开可复现。
    ``char_visible``（与 char_windows 等长）：False = 空白字符（空格等
    无字形内容）——唱字粒子跳过（无走字内容），出入场仍整行参与。
    ``char_styles``（可选，与 char_windows 等长）：逐字符的角色样式
    （调用方按 ``role_label`` 解析后传入）——「跟随字体」按**当前字符**
    自己的角色配色取色；未提供时回落行样式（歌手行已合并歌手方案）。
    """
    bursts: list[dict[str, object]] = []
    count = max(2, min(64, int(style.fx_particle_count)))
    font_px = max(float(getattr(style, "font_size_px", 0.0) or 0.0), 1.0)
    size = font_px * max(0.05, min(2.0, float(getattr(style, "fx_particle_size_em", 0.40))))
    seed_base = ((int(line_index) & 0xFFFF) * 0x9E37) & 0xFFFFFFFF
    char_count = len(char_windows)
    stagger = (
        RIPPLE_CHAR_STAGGER_MS // (char_count - 1) if char_count > 1 else 0
    )
    # 出入场动画粒子：默认固定档（数量恒固定）；开启「联动入退场」后
    # 颜色与尺寸改吃粒子旋钮（颜色模式同样生效）。编排窗口仍随「入场/
    # 退场动画时长」旋钮等比缩放（0 = 默认 600ms 基准）。
    apply_to_anim = bool(getattr(style, "fx_apply_to_entry_exit", False))
    anim_size = size if apply_to_anim else font_px * ANIM_PARTICLE_SIZE_EM
    anim_count = ANIM_PARTICLE_COUNT

    def _char_style(char_index: int | None) -> Style | None:
        # 逐字解析当前字符的角色样式（调用方按 ``role_label`` 叠加好行
        # 样式后传入）；``char_index is None`` 是历史行锚点星花的兼容
        # 入参（2026-10 星光改逐字符后无生产调用方），回落首字符。
        if char_styles:
            if char_index is None:
                char_index = 0
            if 0 <= char_index < len(char_styles):
                return char_styles[char_index]
        return None

    def _burst_paint(
        char_index: int | None,
        burst_size: float,
        *,
        anim: bool,
        ring: bool = False,
        petal: bool = False,
        snow: bool = False,
        heart: bool = False,
        fire: bool = False,
        ash: bool = False,
        feather: bool = False,
        lightning: bool = False,
    ) -> list[dict[str, object]]:
        """burst 的颜色规格**列表**：固定默认档（按 kind 的默认双色）/ 实色
        回退 (+ 取色层级的装饰规格) / 双色档的变体规格列表
        （见 :func:`particle_variant_paints`）。

        规格按「样式 × 角色方案 × 尺寸 × 是否涟漪」缓存——同一组合全帧
        复用一份（见 :func:`_cached_particle_paint`）。涟漪光环不带描边
        （``include_strokes=False``）：环体是发丝线，叠描边显著变粗。
        取色层级（``fx_particle_color_layers``，仅来源配色四档生效）：默认
        仅实色 = 纯色剪影；+描边 = 按方案描边栈（不加装饰）；+装饰 = 仅
        装饰层（不加描边，阴影/发光按方案）；全有 = 描边栈 + 装饰层（见
        :func:`_state_paint_spec`）。

        入退场星光（2026-10 结构统一）同为逐字符 burst，走本函数的完整
        解析——图片/跟随前后/follow_mix/指定方案与取色层级全部生效，
        不再输出 ``char_colors`` 专用颜色表（后端保留读取兼容）。
        """

        if anim and not apply_to_anim:
            # 固定默认档 = 按 kind 的默认双色随机混发（2026-10 用户口径：
            # 出入场粒子用「默认颜色」而非单独颜色的默认白；樱花粉为既有
            # 口径，雪花/爱心/火焰按 kind 各取默认对）。
            pair = _default_color_pair(
                snow, heart, fire, ash, feather, lightning
            )
            return [{"color": pair[0]}, {"color": pair[1]}]
        char_style = _char_style(char_index)
        variants = _cached_particle_variants(
            style,
            char_style,
            burst_size,
            petal=petal,
            snow=snow,
            heart=heart,
            fire=fire,
            ash=ash,
            feather=feather,
            lightning=lightning,
            include_strokes=not ring,
        )
        if variants is not None:
            return variants
        fields: dict[str, object] = {
            "color": _cached_particle_solid(style, char_style)
        }
        spec = _cached_particle_paint(
            style, char_style, burst_size, include_strokes=not ring
        )
        if spec is not None:
            fields["paint"] = spec
        return [fields]

    def _variant_bursts(
        fields: dict[str, object],
        paints: list[dict[str, object]],
        *,
        variant_salt: int = 0,
    ) -> None:
        """按颜色变体发射 burst：单色一条原样下发；双色档拆两条同轨迹
        变体——数量对半（余数归第一条）、种子按变体序号偏移，保证两条
        变体的粒子伪随机位置互相独立（同 index 撞位）。「每颗粒子随机
        取一色」由此实现，渲染端零特殊分支。

        ``total`` 为 1 时无法对半：按 ``variant_salt``（逐字星光传字符
        序）确定性轮换保留哪个变体——第二种颜色/图片跨字符仍会出现，
        而不是固定丢弃（salt=0 保持首变体，兼容非逐字调用方）。"""

        total = int(fields["count"])
        for variant, paint in enumerate(paints):
            burst = {**fields, **paint}
            if len(paints) > 1:
                if total <= 1:
                    if variant != variant_salt % len(paints):
                        continue
                else:
                    # 多色阶按变体数均分（余数给靠前/外层）：火焰 v3 四层
                    # 色阶外多内少，等同按权重 4/4/3/3 分发。
                    base, extra = divmod(total, len(paints))
                    count = base + (1 if variant < extra else 0)
                    # 跳过空 burst：不下发无粒子窗口。
                    if count <= 0:
                        continue
                    burst["count"] = count
                burst["seed"] = (
                    int(fields["seed"]) + variant * 0x9E37
                ) & 0xFFFFFFFF
            bursts.append(burst)

    def _char_time_offset(char_index: int) -> int:
        """逐字时间偏移（毫秒）：固定 20 数表按字符序循环取用。

        2026-10-18 用户口径：不要用哈希生成固定范围，而是把 20 个
        [-80,80] 随机数固化在代码里；所有逐字粒子特效共用同一组偏移，
        第 20 个字回到表首。
        """
        return CHAR_TIME_OFFSETS_MS[
            char_index % len(CHAR_TIME_OFFSETS_MS)
        ]

    def _fire_layer_paints(
        char_index: int,
        burst_size: float,
        travel: float,
        *,
        fixed_default: bool,
    ) -> list[dict[str, object]]:
        """火焰 v3 的四层色阶 paint 列表（外红边 → 橙 → 金 → 白热芯）。

        ``fixed_default``：出入场固定默认档（未开「联动入退场」）——恒用
        火色阶，不吃单独颜色槽；唱字/联动档按用户配色模式解析。模式为
        ``color`` 且双槽仍是默认白时也用火色阶（否则唱字默认喷白粒子，
        读不出火）；用户自定义双色时按四层尺寸循环使用其两色；跟随字体
        等带 ``paint`` 规格的档保持既有完整解析，不逐层改尺寸（规格里的
        描边宽按 burst_size 预算，改尺寸会破坏描边比例）。
        """
        mode = str(
            getattr(style, "fx_particle_color_mode", "color") or "color"
        )
        c1 = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
        c2 = str(getattr(style, "fx_particle_color2", "") or "#FFFFFF")
        default_white = (
            c1.upper() == "#FFFFFF" and c2.upper() == "#FFFFFF"
        )
        use_palette = (
            fixed_default
            or mode == "sakura"
            or (mode == "color" and default_white)
        )
        if use_palette:
            return [
                {
                    "color": color,
                    "size_px": burst_size * size_scale,
                    "travel_px": travel * travel_scale,
                    "kind": layer_kind,
                }
                for color, size_scale, travel_scale, layer_kind
                in FIRE_LAYERS
            ]
        if mode == "color":
            colors = (c1, c2)
            return [
                {
                    "color": colors[index % 2],
                    "size_px": burst_size * size_scale,
                    "travel_px": travel * travel_scale,
                    "kind": layer_kind,
                }
                for index, (_color, size_scale, travel_scale, layer_kind)
                in enumerate(FIRE_LAYERS)
            ]
        return _burst_paint(
            char_index, burst_size, anim=not fixed_default, fire=True
        )

    def _fire_glow_color(
        char_index: int,
        burst_size: float,
        travel: float,
        *,
        fixed_default: bool,
    ) -> str:
        """背光取火色阶第二层（橙）——比红边亮、比白芯柔，压字后不刺眼。"""
        paints = _fire_layer_paints(
            char_index, burst_size, travel, fixed_default=fixed_default
        )
        if len(paints) > 1:
            return str(paints[1].get("color") or FIRE_OUTER)
        return str(paints[0].get("color") or FIRE_OUTER)

    def _emit_flame_bursts(
        char_index: int,
        start_ms: int,
        *,
        size: float,
        travel: float,
        count: int,
        puffs: int,
        fixed_default: bool,
        seed_offset: int,
        puff_ms: int = FLAME_PUFF_MS,
        with_glow: bool = True,
        with_embers: bool = True,
        end_limit_ms: int | None = None,
    ) -> None:
        """按“连续火流”发射火焰 v3：同一字符窗口内分 ``puffs`` 簇错峰，
        每簇覆盖四层色阶 + 背光 + 余烬。

        出入场由调用方按「入场/退场动画时长」计算 puffs/puff_ms，并传
        ``end_limit_ms``（文字全部出现/消失后 260ms）——所有火粒/背光/
        余烬的 burst 窗口在终点前自然走到 p=1，透明度归零，不会硬截断。
        唱字按字窗长度补簇（不传 limit，长音持续喷发）。
        """
        puffs = max(1, int(puffs))
        total = max(1, int(count))
        base, extra = divmod(total, puffs)

        def _clip_end(start: int, life_ms: int) -> int | None:
            end = int(start) + int(life_ms)
            if end_limit_ms is not None:
                end = min(end, int(end_limit_ms))
            # 太短的窗口连一次淡入淡出都走不完，直接不下发。
            return end if end - int(start) >= 80 else None

        glow_color = (
            _fire_glow_color(
                char_index, size, travel, fixed_default=fixed_default
            )
            if with_glow
            else FIRE_OUTER
        )
        glow_size = size * FLAME_GLOW_SIZE_SCALE
        ember_size = size * 0.20
        for puff in range(puffs):
            puff_count = base + (1 if puff < extra else 0)
            if puff_count <= 0:
                continue
            start = int(start_ms) + int(puff_ms) * puff
            seed = (
                seed_base + seed_offset + char_index * 7 + puff * 0x51
            ) & 0xFFFFFFFF
            flame_end = _clip_end(start, FLAME_LIFE_MS)
            if flame_end is None:
                continue
            _variant_bursts({
                "kind": "flame", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": flame_end,
                "count": puff_count, "seed": seed,
                "size_px": size, "travel_px": travel,
                "front": True, "sweep": 0,
            }, _fire_layer_paints(
                char_index, size, travel, fixed_default=fixed_default
            ))
            if with_glow:
                glow_end = _clip_end(start, FLAME_GLOW_LIFE_MS)
                if glow_end is not None:
                    _variant_bursts({
                        "kind": "flame_glow", "anchor": "char",
                        "char_index": int(char_index),
                        "start_ms": start,
                        "end_ms": glow_end,
                        "count": 1, "seed": (seed + 0x31) & 0xFFFFFFFF,
                        "size_px": glow_size,
                        "travel_px": size * 2.0,
                        "front": False, "sweep": 0,
                    }, [{
                        "color": glow_color,
                        "size_px": glow_size,
                        "travel_px": size * 2.0,
                    }])
            if with_embers:
                ember_start = start + 40
                ember_end = _clip_end(ember_start, EMBER_LIFE_MS)
                if ember_end is not None:
                    _variant_bursts({
                        "kind": "ember", "anchor": "char",
                        "char_index": int(char_index),
                        "start_ms": ember_start,
                        "end_ms": ember_end,
                        "count": 2, "seed": (seed + 0x57) & 0xFFFFFFFF,
                        "size_px": ember_size,
                        "travel_px": size * 2.6,
                        "front": True, "sweep": 0,
                    }, [{"color": EMBER_A}, {"color": EMBER_B}])

    def _emit_lightning_bursts(
        char_index: int,
        start_ms: int,
        *,
        size: float,
        count: int,
        window_ms: int,
        fixed_default: bool,
        seed_offset: int,
        with_glow: bool = True,
    ) -> None:
        """发射一组闪电（AE Advanced Lightning 语言）：核心 bolt 画在文字
        前，同种子、同数量、放大 1.35× 的暗色 bolt 画在文字后当辉光。

        核心与辉光使用同一 ``seed``/``count`` 与同一求值公式，逐颗粒子的
        位置/旋转/时序完全重合；只有尺寸与透明度不同（求值器按 kind
        分支），因此不会出现“辉光错位”。
        """
        total = max(1, int(count))
        # 唱字/联动档的颜色默认是「单独颜色」的双白；白闪电会读成普通
        # 闪白，不像电——与火焰 v3 同口径：双槽仍是默认白时改用闪电色阶。
        mode = str(
            getattr(style, "fx_particle_color_mode", "color") or "color"
        )
        c1 = str(getattr(style, "fx_particle_color", "") or "#FFFFFF")
        c2 = str(getattr(style, "fx_particle_color2", "") or "#FFFFFF")
        if (
            not fixed_default
            and mode == "color"
            and c1.upper() == "#FFFFFF"
            and c2.upper() == "#FFFFFF"
        ):
            paints: list[dict[str, object]] = [
                {"color": LIGHTNING_A}, {"color": LIGHTNING_B}
            ]
        else:
            paints = _burst_paint(
                char_index, size, anim=fixed_default, lightning=True
            )
        core = {
            "kind": "lightning", "anchor": "char",
            "char_index": int(char_index),
            "start_ms": int(start_ms),
            "end_ms": int(start_ms) + max(int(window_ms), 120),
            "count": total,
            "seed": (seed_base + seed_offset + char_index) & 0xFFFFFFFF,
            "size_px": size, "travel_px": 0.0,
            "front": True, "sweep": 0,
        }
        _variant_bursts(core, paints)
        if with_glow:
            glow_size = size * LIGHTNING_GLOW_SIZE_SCALE
            _variant_bursts({
                **core,
                "kind": "lightning_glow",
                "size_px": glow_size,
                "front": False,
            }, paints)

    def _duration_scale(configured: int) -> float:
        total = min(max(int(configured), 120), 3000)
        return total / 600.0

    # 旧语义：时长 0 = 无动画（粒子同样不发）。
    entry_active = int(getattr(style, "entry_lead_ms", 0) or 0) > 0
    exit_active = int(getattr(style, "exit_fade_ms", 0) or 0) > 0
    entry_scale = _duration_scale(
        int(getattr(style, "entry_lead_ms", 0) or 0)
    )
    exit_scale = _duration_scale(int(getattr(style, "exit_fade_ms", 0) or 0))

    def _char_ripples(
        base_ms: int,
        seed_offset: int,
        *,
        stagger_ms: int,
        ring_size: float | None = None,
        scale: float = 1.0,
    ) -> None:
        px = anim_size * 3.6 if ring_size is None else ring_size
        for char_index in range(char_count):
            start = (
                int(base_ms)
                + int(stagger_ms * scale) * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(RIPPLE_LIFE_MS * scale),
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + seed_offset + char_index) & 0xFFFFFFFF,
                # 水波纹基准直径 ≈ 1.4× 字高，扩散峰值约 1.6× 字高。
                "size_px": px, "travel_px": 0.0, "front": False,
                "sweep": 0,
            }, _burst_paint(char_index, px, anim=True, ring=True))

    def _sparkle_char_bursts(
        *,
        base_ms: int,
        window_end_ms: int,
        scale: float,
        seed_offset: int,
        reverse: bool,
        char_life_ms: float | None = None,
    ) -> None:
        """入/退场星光（2026-10 结构统一）：逐字符生成 ``anchor="char"``
        的粒子组，外观复用 ``_burst_paint`` 完整解析（图片/跟随前后/
        follow_mix/指定方案/取色层级与其余字符锚点粒子同一套规则，
        多角色行每字跟自己的方案）；粒子出生即固定归属出生字符，漂移
        到邻字附近也不换角色。

        整行「左→右入场 / 右→左退场」的扫过编排由逐字错峰实现：总扫过
        时长仍为 ``SPARKLE_SWEEP_MS * scale``，逐字间隔 = 总时长 /
        (槽数-1)；但错峰总量受窗口钳制——必须给最晚启动的字符留出「随机
        出生延迟上限 + 最短可见寿命」，短退场窗口宁可放弃扫过也不让粒子
        整组无法出生（求值器对随机延迟另有同口径二次钳制，见 sparkle
        分支 sweep=0）。窗口终点与旧行锚点档同一条（入场 = 显示窗起点 +
        SPARKLE_ENTRY_LIFE_MS * scale，退场 = 显示窗终点），越晚启动的
        字符窗口越短——与旧档「扫过延迟吃掉剩余寿命」的节奏同构，总
        跨度不变。数量按整行预算 ``anim_count`` 以「粒子major」网格
        均匀撒到整行字符槽（第 i 颗落在第 round(i·(n-1)/(k-1)) 槽）：
        预算少于槽数时两端字符必有粒子、中间隔位轮空，不再集中分给
        前几个字符（长行行尾/star、反向退场起点不再永久无星光）；预算
        多于槽数时每槽 1~2 颗。双颜色变体在 ``_variant_bursts`` 内再
        对半拆分（单颗时按字符序轮换变体），不重复加量。空格无字形
        pivot，不参与（唱字粒子同口径）。
        """
        slots = (
            [
                index
                for index in range(min(char_count, len(char_visible)))
                if char_visible[index]
            ]
            if char_visible is not None
            else list(range(char_count))
        )
        if not slots:
            return
        steps = max(len(slots) - 1, 1)
        stagger = int(SPARKLE_SWEEP_MS * scale) // steps
        if char_life_ms is None:
            # 共享窗终点模式（入场标准窗）：预留出生+显示时间，错峰吃掉
            # 太多会让最晚字符的窗口容不下随机出生延迟。
            stagger_budget = max(
                int(window_end_ms) - int(base_ms)
                - int(SPARKLE_BIRTH_JITTER_MS)
                - int(SPARKLE_MIN_VISIBLE_MS),
                0,
            )
            stagger = min(stagger, stagger_budget // steps)
        budget = max(anim_count, 1)
        counts = [0] * len(slots)
        for i in range(budget):
            counts[
                round(i * (len(slots) - 1) / max(budget - 1, 1))
            ] += 1
        for position, char_index in enumerate(slots):
            count = counts[position]
            if count <= 0:
                continue
            order = (
                len(slots) - 1 - position if reverse else position
            )
            start = (
                int(base_ms)
                + stagger * order
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "sparkle", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": (
                    start + int(char_life_ms)
                    if char_life_ms is not None
                    else max(int(window_end_ms), start + 1)
                ),
                "count": count,
                "seed": (seed_base + seed_offset + char_index) & 0xFFFFFFFF,
                # 星光画在主文字前（背后看不清——2026-10 用户复调；纵向
                # 锚点已偏置到字上侧为主，遮挡有限）。
                "size_px": anim_size, "travel_px": anim_size * 3.2,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True),
                variant_salt=char_index)

    entry_anim = str(getattr(style, "entry_anim", "none") or "none")
    if entry_anim == "sparkle" and entry_active and display_start_ms is not None:
        # 星光入场：逐字符 burst（外观/角色归属与其余粒子同构），整行
        # 左→右扫过由逐字错峰实现（总时长 SPARKLE_SWEEP_MS 等比）。
        _sparkle_char_bursts(
            base_ms=int(display_start_ms),
            window_end_ms=int(display_start_ms) + SPARKLE_ENTRY_LIFE_MS,
            scale=1.0,
            seed_offset=1,
            reverse=False,
        )
    elif entry_anim == "ripple" and entry_active and display_start_ms is not None:
        _char_ripples(
            int(display_start_ms), 2, stagger_ms=stagger, scale=1.0
        )
    elif entry_anim == "note" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + SING_NOTE_TOTAL_MS,
                "count": 3, "seed": (seed_base + 6 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True))
    elif entry_anim == "heart" and entry_active and display_start_ms is not None:
        # 爱心飘出（2026-10 用户口径：全面克隆音符飘出，只换形状与默认
        # 红/粉双色）——逐字错峰、行程 1.8×尺寸、字顶出生上升摆动全同款。
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "heart", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + SING_NOTE_TOTAL_MS,
                "count": 3, "seed": (seed_base + 14 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True, heart=True))
    elif entry_anim == "flame" and entry_active and display_start_ms is not None:
        # 火焰燃起（v3，2026-10-18 用户口径更新）：火粒总长跟随「入场
        # 动画时长」；逐字错峰与文字 char-state 同构（duration×7/12），
        # 文字全部出现后再保留 260ms 让火快速熄灭。
        flame_size = font_px * 0.42
        flame_duration = min(
            max(int(getattr(style, "entry_lead_ms", 0) or 0), 120), 3000
        )
        flame_stagger = (
            (flame_duration * 7 // 12) // max(char_count - 1, 1)
            if char_count > 1
            else 0
        )
        flame_puffs = max(
            2,
            min(
                FLAME_MAX_PUFFS,
                math.ceil(flame_duration / FLAME_PUFF_MS),
            ),
        )
        flame_puff_ms = max(1, flame_duration // flame_puffs)
        flame_count = max(
            FLAME_MIN_COUNT,
            min(
                FLAME_MAX_COUNT,
                round(ANIM_FLAME_COUNT * flame_duration / 600),
            ),
        )
        fire_end = int(display_start_ms) + flame_duration + FLAME_TAIL_MS
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + flame_stagger * char_index
                + _char_time_offset(char_index)
            )
            _emit_flame_bursts(
                char_index,
                start,
                size=flame_size,
                travel=flame_size * 3.4,
                count=flame_count,
                puffs=flame_puffs,
                puff_ms=flame_puff_ms,
                fixed_default=not apply_to_anim,
                seed_offset=16,
                end_limit_ms=fire_end,
            )
    elif entry_anim == "feather" and entry_active and display_start_ms is not None:
        # 羽毛飘入（2026-10，AE 羽毛参考）：逐字错峰自字顶上方飘落，
        # 宽幅钟摆横摆 + 连续翻转 + 横向压扁（羽片侧对镜头），慢速降落
        # 到字底后淡出。用固定标准档（与花瓣/雪花同口径）。
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "feather", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + FEATHER_LIFE_MS,
                "count": 3, "seed": (seed_base + 20 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": 1,
            }, _burst_paint(
                char_index, anim_size, anim=True, feather=True
            ))
    elif entry_anim == "lightning" and entry_active and display_start_ms is not None:
        # 闪电劈落（2026-10，AE Advanced Lightning 参考）：逐字错峰，
        # 每字窗口内多次随机重击；核心 bolt 前置、暗色放大副本垫后。
        lightning_size = anim_size * LIGHTNING_SIZE_SCALE
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _emit_lightning_bursts(
                char_index,
                start,
                size=lightning_size,
                count=ANIM_LIGHTNING_STRIKES,
                window_ms=LIGHTNING_WINDOW_MS,
                fixed_default=not apply_to_anim,
                seed_offset=21,
            )
    elif entry_anim == "petal" and entry_active and display_start_ms is not None:
        # 花瓣飘入：每字自字形顶上方错峰飘落汇拢到字框（ease-out 减速
        # 抵达后熄灭），随机摇摆 + 连续翻转（\moves4 四点飘移等效观感）。
        for char_index in range(char_count):
            start = (
                int(display_start_ms)
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + PETAL_LIFE_MS,
                "count": 3, "seed": (seed_base + 10 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": 1,
            }, _burst_paint(char_index, anim_size, anim=True, petal=True))
    elif entry_anim in SNOW_KINDS and entry_active and display_start_ms is not None:
        # 雪花飘入（2026-10-18 用户口径：雪要慢慢落、至少落到字底）：
        # 提前 350ms 起飘、整段落 1800ms（旧 1050ms 太快，过半字就淡没）；
        # 逐字 ±80ms 时间抖动；终点覆盖到主文字底部之后，文字显形时雪仍
        # 在半空继续落。
        snow_start = max(0, int(display_start_ms) - SNOW_ENTRY_LEAD_MS)
        snow_end = snow_start + SNOW_ENTRY_FALL_MS
        for char_index in range(char_count):
            _variant_bursts({
                "kind": entry_anim, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": snow_start + _char_time_offset(char_index),
                "end_ms": snow_end,
                "count": 3, "seed": (seed_base + 12 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": 1,
            }, _burst_paint(char_index, anim_size, anim=True, snow=True))

    exit_anim = str(getattr(style, "exit_anim", "none") or "none")
    if exit_anim == "sparkle" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - SPARKLE_EXIT_LIFE_MS,
        )
        # 星光退场：逐字符 burst，整行右→左扫过（最右侧字符先启动）。
        # 每字完整寿命——行消失后由渲染端退场尾奏通道续播到自然终点。
        _sparkle_char_bursts(
            base_ms=exit_start,
            window_end_ms=int(display_end_ms),
            scale=1.0,
            seed_offset=3,
            reverse=True,
            char_life_ms=SPARKLE_EXIT_LIFE_MS,
        )
    elif exit_anim == "ripple" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - (RIPPLE_CHAR_STAGGER_MS + RIPPLE_LIFE_MS),
        )
        # 每环完整寿命、标准错峰——行消失后由退场尾奏通道续播。
        _char_ripples(exit_start, 4, stagger_ms=stagger, scale=1.0)
    elif exit_anim == "note" and exit_active and display_end_ms is not None:
        exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - (RIPPLE_CHAR_STAGGER_MS + SING_NOTE_TOTAL_MS),
        )
        for char_index in range(char_count):
            start = (
                exit_start
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "note", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + SING_NOTE_TOTAL_MS,
                "count": 3, "seed": (seed_base + 8 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True))
    elif exit_anim == "heart" and exit_active and display_end_ms is not None:
        # 爱心飘出（退场，克隆音符）——同窗反向错峰、行程/数量/寿命全同款。
        exit_start_heart = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - (RIPPLE_CHAR_STAGGER_MS + SING_NOTE_TOTAL_MS),
        )
        for char_index in range(char_count):
            start = (
                exit_start_heart
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "heart", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + SING_NOTE_TOTAL_MS,
                "count": 3, "seed": (seed_base + 15 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": anim_size * 1.8,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size, anim=True, heart=True))
    elif exit_anim == "flame" and exit_active and display_end_ms is not None:
        # 火焰烧尽（v3，2026-10-18 用户口径更新）：火粒总长跟随「退场
        # 动画时长」，逐字错峰与文字 char-state 同构；文字全部消失后
        # 只保留 260ms 短尾，火焰快速熄灭（不再续播 1s+）。
        flame_size = font_px * 0.42
        flame_duration = min(
            max(int(getattr(style, "exit_fade_ms", 0) or 0), 120), 3000
        )
        flame_stagger = (
            (flame_duration * 7 // 12) // max(char_count - 1, 1)
            if char_count > 1
            else 0
        )
        flame_puffs = max(
            2,
            min(
                FLAME_MAX_PUFFS,
                math.ceil(flame_duration / FLAME_PUFF_MS),
            ),
        )
        flame_puff_ms = max(1, flame_duration // flame_puffs)
        flame_count = max(
            FLAME_MIN_COUNT,
            min(
                FLAME_MAX_COUNT,
                round(ANIM_FLAME_COUNT * flame_duration / 600),
            ),
        )
        exit_start_flame = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - flame_duration,
        )
        fire_end = int(display_end_ms) + FLAME_TAIL_MS
        for char_index in range(char_count):
            start = (
                exit_start_flame
                + flame_stagger * char_index
                + _char_time_offset(char_index)
            )
            _emit_flame_bursts(
                char_index,
                start,
                size=flame_size,
                travel=flame_size * 3.4,
                count=flame_count,
                puffs=flame_puffs,
                puff_ms=flame_puff_ms,
                fixed_default=not apply_to_anim,
                seed_offset=17,
                end_limit_ms=fire_end,
            )
            ash_end = min(start + ASH_LIFE_MS, fire_end)
            if ash_end - start >= 80:
                _variant_bursts({
                    "kind": "ash", "anchor": "char",
                    "char_index": int(char_index),
                    "start_ms": start,
                    "end_ms": ash_end,
                    "count": 3,
                    "seed": (seed_base + 18 + char_index) & 0xFFFFFFFF,
                    "size_px": font_px * 0.14, "travel_px": 0.0,
                    "front": True, "sweep": 0,
                }, _burst_paint(
                    char_index, font_px * 0.14, anim=True, ash=True
                ))
    elif exit_anim == "feather" and exit_active and display_end_ms is not None:
        # 羽毛飘散：自字框内错峰剥落，钟摆式大幅横摆 + 翻转飘向字底
        # 下方淡出（慢于花瓣，落点更低）。
        feather_exit_start = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - (RIPPLE_CHAR_STAGGER_MS + FEATHER_LIFE_MS),
        )
        for char_index in range(char_count):
            start = (
                feather_exit_start
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "feather", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + FEATHER_LIFE_MS,
                "count": 3, "seed": (seed_base + 22 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": -1,
            }, _burst_paint(
                char_index, anim_size, anim=True, feather=True
            ))
    elif exit_anim == "lightning" and exit_active and display_end_ms is not None:
        # 闪电消散：文字消失前自每个字连劈数次，随后 bolt 与暗色辉光
        # 一起熄灭（尾奏通道续播到窗口终点）。
        lightning_size = anim_size * LIGHTNING_SIZE_SCALE
        exit_start_lightning = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - (RIPPLE_CHAR_STAGGER_MS + LIGHTNING_WINDOW_MS),
        )
        for char_index in range(char_count):
            start = (
                exit_start_lightning
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _emit_lightning_bursts(
                char_index,
                start,
                size=lightning_size,
                count=ANIM_LIGHTNING_STRIKES,
                window_ms=LIGHTNING_WINDOW_MS,
                fixed_default=not apply_to_anim,
                seed_offset=23,
            )
    elif exit_anim == "petal" and exit_active and display_end_ms is not None:
        exit_start_petal = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms) - (RIPPLE_CHAR_STAGGER_MS + PETAL_LIFE_MS),
        )
        # 花瓣飘散：每字自字框内错峰剥落、边翻转边飘落到字底下方淡出。
        for char_index in range(char_count):
            start = (
                exit_start_petal
                + stagger * char_index
                + _char_time_offset(char_index)
            )
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + PETAL_LIFE_MS,
                "count": 3, "seed": (seed_base + 11 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": -1,
            }, _burst_paint(char_index, anim_size, anim=True, petal=True))
    elif exit_anim in SNOW_KINDS and exit_active and display_end_ms is not None:
        # 雪花飘散（2026-10-18 用户口径：慢落、至少到字底）：落窗固定
        # 1800ms，从显示末前 1.5s 起飘（旧锚在行末只剩 400ms 的快速
        # 坠落）；终点晚于显示窗 300ms，尾奏通道续播到自然熄灭。
        snow_exit_end = int(display_end_ms) + 300
        snow_exit_start = max(
            int(display_start_ms) if display_start_ms is not None else 0,
            snow_exit_end - SNOW_EXIT_FALL_MS,
        )
        for char_index in range(char_count):
            _variant_bursts({
                "kind": exit_anim, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": snow_exit_start
                + _char_time_offset(char_index),
                "end_ms": snow_exit_end,
                "count": 3, "seed": (seed_base + 13 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size, "travel_px": 0.0,
                "front": True, "sweep": -1,
            }, _burst_paint(char_index, anim_size, anim=True, snow=True))

    # 粒子拼接/消散：像素风方块粒子，同出入场动画档（默认固定档）。
    per_char_assemble = max(4, ANIM_PARTICLE_COUNT // 2)
    if entry_anim == "assemble_in" and entry_active and display_start_ms is not None:
        for char_index in range(char_count):
            start = int(display_start_ms) + int(stagger * entry_scale) * char_index
            _variant_bursts({
                "kind": "assemble", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * entry_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 7 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size * 0.75, anim=True))
    if exit_anim == "dissolve_out" and exit_active and display_end_ms is not None:
        exit_start_assemble = max(
            int(line_end_ms) if line_end_ms is not None else 0,
            int(display_end_ms)
            - int((RIPPLE_CHAR_STAGGER_MS + ASSEMBLE_LIFE_MS) * exit_scale),
        )
        tail = (
            int(display_end_ms) - exit_start_assemble
            - int(ASSEMBLE_LIFE_MS * exit_scale)
        )
        stagger_dissolve = min(
            int(stagger * exit_scale),
            max(0, tail) // max(1, char_count - 1),
        )
        for char_index in range(char_count):
            start = exit_start_assemble + stagger_dissolve * char_index
            _variant_bursts({
                "kind": "dissolve", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": start,
                "end_ms": start + int(ASSEMBLE_LIFE_MS * exit_scale),
                "count": per_char_assemble,
                "seed": (seed_base + 9 + char_index) & 0xFFFFFFFF,
                "size_px": anim_size * 0.75,
                "travel_px": font_px * ASSEMBLE_TRAVEL_EM,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, anim_size * 0.75, anim=True))

    # 唱字装饰粒子：默认唯一吃粒子旋钮（尺寸/数量/颜色）的档位。
    if style.sing_fx == "ripple":
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            ring_px = size * 3.6
            _variant_bursts({
                "kind": "ripple", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + RIPPLE_LIFE_MS,
                "count": RIPPLE_RING_COUNT,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": ring_px, "travel_px": 0.0, "front": False,
                "sweep": 0,
            }, _burst_paint(char_index, ring_px, anim=False, ring=True))
    elif style.sing_fx == "petal":
        # 花瓣飘动：唱到的字上花瓣轻摆缓沉（数量吃粒子旋钮，默认 4/字），
        # 窗口不受唱字窗约束、自然播完（与星光/音符同口径）。
        per_char = max(3, count // 3)
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _variant_bursts({
                "kind": "petal", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + PETAL_LIFE_MS,
                "count": per_char,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size, "travel_px": 0.0,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, size, anim=False, petal=True))
    elif style.sing_fx == "feather":
        # 羽毛飘动（唱字）：唱到的字上羽毛缓摆缓沉（数量吃粒子旋钮，
        # 默认 4/字），窗口不受唱字窗约束、自然播完（与花瓣同口径）。
        per_char = max(3, count // 3)
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _variant_bursts({
                "kind": "feather", "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + FEATHER_LIFE_MS,
                "count": per_char,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size, "travel_px": 0.0,
                "front": True, "sweep": 0,
            }, _burst_paint(
                char_index, size, anim=False, feather=True
            ))
    elif style.sing_fx == "lightning":
        # 闪电闪烁（唱字）：自字窗起点连劈一阵（窗口最长 1.2s），数量
        # 吃粒子旋钮；核心 bolt 前置、暗色辉光垫后，结束后自然熄灭。
        per_char = max(3, min(10, count // 3))
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _emit_lightning_bursts(
                char_index,
                int(start_ms),
                size=size * LIGHTNING_SIZE_SCALE,
                count=per_char,
                window_ms=min(max(duration, 360), 1200),
                fixed_default=False,
                seed_offset=24,
            )
    elif style.sing_fx in SNOW_KINDS:
        # 雪花飘动：唱到的字上自 ruby 锚出生的雪花匀速缓沉穿过字框
        #（数量吃粒子旋钮，默认 4/字；尺寸随下落由大变小——轨迹口径），
        # 窗口不受唱字窗约束、自然播完（与花瓣同口径）。
        per_char = max(3, count // 3)
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            _variant_bursts({
                "kind": style.sing_fx, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + SNOW_LIFE_MS,
                "count": per_char,
                "seed": (seed_base + 5 + char_index) & 0xFFFFFFFF,
                "size_px": size, "travel_px": 0.0,
                "front": True, "sweep": 0,
            }, _burst_paint(char_index, size, anim=False, snow=True))
    elif style.sing_fx == "flame":
        # 火焰燃烧（唱字，v3）：字窗内**持续喷发**——按字长每 ~280ms 一簇
        # （长音最多 8 簇），每簇四层色阶 + 背光 + 余烬；总颗数吃粒子旋钮
        # 且随簇数抬升（长音不能只喷字头一次）。窗口自然播完。
        flame_sing_size = size * 1.2
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            puffs = max(
                1,
                min(
                    FLAME_MAX_PUFFS,
                    math.ceil(duration / FLAME_PUFF_MS),
                ),
            )
            puff_ms = max(1, duration // puffs)
            sing_total = min(40, max(count, puffs * 6))
            _emit_flame_bursts(
                char_index,
                int(start_ms),
                size=flame_sing_size,
                travel=flame_sing_size * 3.4,
                count=sing_total,
                puffs=puffs,
                puff_ms=puff_ms,
                fixed_default=False,
                seed_offset=7,
            )
    elif style.sing_fx in ("twinkle", "twinkle_classic", "note", "heart"):
        # 唱字星光并入出入场星光运动学后，密度对齐出入场观感（默认 7/字）；
        # 「旧版」档（twinkle_classic）保留 2026-10 运动学改造前的原地闪烁
        # 观感，颗数沿用旧口径（默认 3/字）。
        if style.sing_fx == "twinkle":
            per_char = max(5, count // 2)
        elif style.sing_fx == "twinkle_classic":
            per_char = max(3, count // 4)
        else:
            per_char = 3
        total = (
            SING_NOTE_TOTAL_MS
            if style.sing_fx in ("note", "heart")
            else SING_TWINKLE_TOTAL_MS
        )
        for char_index, (start_ms, end_ms) in enumerate(char_windows):
            duration = int(end_ms) - int(start_ms)
            if duration <= 0:
                continue
            if char_visible is not None and not char_visible[char_index]:
                continue
            seed = (seed_base + 5 + char_index) & 0xFFFFFFFF
            _variant_bursts({
                # 叠加在正在唱的那个字上（用户口径）：锚点=该字符自身
                # 中心；发射时刻=该字符唱字窗起点，窗口延伸到轨迹自然
                # 播完（换字后余韵继续，不硬截断）。
                "kind": style.sing_fx, "anchor": "char",
                "char_index": int(char_index),
                "start_ms": int(start_ms),
                "end_ms": int(start_ms) + total,
                "count": per_char,
                "seed": seed,
                # 星光族画主文字前（背后看不清——2026-10 用户复调）；
                # 唱字星光自字左向右小扫过（出入场同款波向运动学）；
                # 旧版档原地闪烁（sweep=0，无扫过）。
                "size_px": size, "travel_px": size * 1.8, "front": True,
                "sweep": 1 if style.sing_fx == "twinkle" else 0,
            }, _burst_paint(
                char_index, size, anim=False,
                heart=style.sing_fx == "heart",
            ))
    return bursts


# ---------------------------------------------------------------------------
# 轨迹求值：t 的纯函数（Python/C++ 镜像；全部 double 运算）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParticleDraw:
    """一颗粒子在 t 时刻的绘制状态。

    ``stretch_x/stretch_y``：sprite 双轴缩放倍率（火焰火舌随上升拉长、
    变窄的“速度拉伸”；其余 kind 恒 1.0）。两后端分别作用在 sprite 的
    X/Y 缩放上，不是尺寸字段的替代——``size_px`` 仍是基准高度。
    """

    x: float
    y: float
    rotation_deg: float
    size_px: float
    alpha: float
    stretch_x: float = 1.0
    stretch_y: float = 1.0


def burst_particles_at(
    burst: dict[str, object],
    t_ms: int,
    origin_x: float,
    origin_y: float,
    box_w: float,
    box_h: float,
) -> list[ParticleDraw]:
    """求值一个 burst 在 ``t_ms`` 的全部粒子。窗口外返回空。

    ``origin_x/origin_y``：burst 锚点（行中心或字符中心，调用方布局解析）。
    ``box_w/box_h``：锚点盒（twinkle 的散布范围 / note 的上升行程参照）。
    """
    start_ms = int(burst["start_ms"])
    end_ms = int(burst["end_ms"])
    if t_ms < start_ms or t_ms > end_ms:
        return []
    kind = str(burst["kind"])
    seed = int(burst["seed"])
    size = float(burst["size_px"])
    travel = float(burst["travel_px"])
    sweep = int(burst.get("sweep", 0) or 0)
    life = max(end_ms - start_ms, 1)
    tau = float(t_ms - start_ms)
    out: list[ParticleDraw] = []

    if kind == "ripple":
        # 水波纹（Aegisub/AE 同款口径）：每个落点 3 枚细描边圆环错峰扩散
        # （第 i 环延迟 150ms），各自「自小圈快速扩开 → 接近满径时淡出」，
        # 呈现一石入水的连漪感；环体是发丝线，不是实心铜钱。
        for i in range(max(1, int(burst["count"]))):
            delay = float(i) * RIPPLE_RING_STAGGER_MS
            life_i = max(life - delay, 1.0)
            p = min(max((tau - delay) / life_i, 0.0), 1.0)
            if p <= 0.0:
                continue
            eased = 1.0 - (1.0 - p) * (1.0 - p)
            scale = size * (0.25 + 0.85 * eased)
            out.append(ParticleDraw(
                origin_x, origin_y, 0.0, scale, (1.0 - p) * (1.0 - p),
            ))
        return out

    star_prev_q = -1  # 星光族象限链：-1 = 尚无上一颗
    for i in range(int(burst["count"])):
        if kind in ("sparkle", "twinkle"):
            # 星光族运动：粒子伪随机铺满锚框宽度（box_w；行锚点=整行、
            # 字符锚点=字框），``sweep`` 非零时按横向位置错峰出生形成顺滑
            # 扫过（入退场星光 2026-10 起改逐字符 burst + 规划期错峰，
            # 此处 sweep=0，字内随机延迟出生）；每颗随机初始/末了大小、
            # 随机向上漂移方向。唱字星光（twinkle）2026-10 起并入同一运
            # 动学（原地 sin 缩放的旧模式僵硬——用户口径），在字框内做
            # 同款小扫过 + 漂移。纵向锚点按 star_y_fraction 偏置到主文字
            # 上侧（只稍微溢出字形顶），且连续两颗不落同象限（链式比较
            # 出生位置，与存活过滤无关、重放可复现）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            left = (u1 - 0.5) < 0.0
            u2 = star_y_resample(
                u2,
                fx_unit_hash(seed + i, 6),
                fx_unit_hash(seed + i, 7),
                star_prev_q,
                left,
            )
            y_off = star_y_fraction(u2) * box_h
            star_prev_q = _star_quadrant(left, y_off < 0.0)
            x_norm = u1
            if sweep > 0:
                delay = x_norm * SPARKLE_SWEEP_MS + u5 * 90.0
            elif sweep < 0:
                delay = (1.0 - x_norm) * SPARKLE_SWEEP_MS + u5 * 90.0
            else:
                # 短窗口（如 120ms 退场）不能让随机出生延迟吞掉整组粒子：
                # 钳到「寿命 - 最短可见寿命」，每颗至少有可见时段。
                delay = min(
                    u5 * SPARKLE_BIRTH_JITTER_MS,
                    max(life - SPARKLE_MIN_VISIBLE_MS, 0.0),
                )
            denom = max(life - delay, 1.0)
            p = min(max((tau - delay) / denom, 0.0), 1.0)
            if p <= 0.0:
                continue
            size_i = size * (0.65 + 0.70 * u3)
            size_f = size_i * (0.15 + 0.30 * u4)
            scale = size_i + (size_f - size_i) * p
            drift_x = (u4 - 0.5) * travel * 0.35 * p
            drift_y = -(0.30 + 0.70 * u2) * travel * 0.45 * p
            out.append(ParticleDraw(
                origin_x + (x_norm - 0.5) * box_w + drift_x,
                origin_y + y_off + drift_y,
                u3 * 90.0 + 60.0 * p,
                scale,
                1.0 - p,
            ))
        elif kind == "twinkle_classic":
            # 旧版唱字星光（2026-10 运动学改造前的形态，逐字复刻自被
            # 2775dc99 删除的 twinkle 分支）：位置对称铺满整个字框、随机
            # 固定旋转、随机寿命（300–450ms）与峰值大小，sin 包络「出生
            # →放大→熄灭」的原地闪烁。镜像 C++ d2d_fx.cpp 同名分支。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            life_i = 300.0 + 150.0 * u4
            spread = max(life - life_i, 0.0)
            delay = u3 * spread * 0.8
            p = min(max((tau - delay) / life_i, 0.0), 1.0)
            if p <= 0.0 or p >= 1.0:
                continue
            envelope = math.sin(math.pi * p)
            peak = size * (0.65 + 0.70 * u4)
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.95,
                origin_y + (u2 - 0.5) * box_h * 0.85,
                u3 * 120.0 - 30.0,
                peak * (0.30 + 0.70 * envelope),
                envelope,
            ))
        elif kind == "assemble":
            # 粒子拼接：每颗粒子自随机方向的远端飞向字形内随机落点，
            # 平滑抵达后缩小熄灭（Trapcode 汇聚语言）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            delay = u3 * 90.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0 or p >= 1.0:
                continue
            eased = p * p * (3.0 - 2.0 * p)
            land_x = (u1 - 0.5) * box_w * 0.9
            land_y = (u4 - 0.5) * box_h * 0.8
            theta = u2 * 2.0 * math.pi
            speed = 0.6 + 0.5 * u4
            start_x = land_x + math.cos(theta) * travel * speed
            start_y = land_y + math.sin(theta) * travel * speed * 0.7
            out.append(ParticleDraw(
                origin_x + start_x + (land_x - start_x) * eased,
                origin_y + start_y + (land_y - start_y) * eased,
                (u3 - 0.5) * 240.0 * (1.0 - eased),
                size * (0.9 - 0.35 * eased),
                min(p * 6.0, 1.0) * (1.0 - p * p),
            ))
        elif kind == "dissolve":
            # 粒子消散：粒子自字形内随机起点飞向随机方向远端并淡出。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            delay = u3 * 70.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            eased = p * p * (3.0 - 2.0 * p)
            start_x = (u1 - 0.5) * box_w * 0.9
            start_y = (u4 - 0.5) * box_h * 0.8
            theta = u2 * 2.0 * math.pi
            speed = 0.6 + 0.5 * u4
            out.append(ParticleDraw(
                origin_x + start_x + math.cos(theta) * travel * speed * eased,
                origin_y + start_y + math.sin(theta) * travel * speed * 0.7 * eased,
                (u3 - 0.5) * 240.0 * eased,
                size * (0.75 + 0.25 * (1.0 - eased)),
                (1.0 - p) * (1.0 - p * 0.5),
            ))
        elif kind in ("note", "heart"):
            # 音符/爱心（爱心克隆音符运动学，2026-10 用户口径）：出生点在
            # 字框顶部随机横移，上升带随机摆动，随机大小。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            delay = u3 * life * 0.35
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            rise = travel * (1.0 - (1.0 - p) * (1.0 - p))
            sway = math.sin((u2 + p * 1.5) * 2.0 * math.pi) * size * 0.4
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.8 + sway,
                origin_y - box_h * 0.35 - rise,
                (u2 - 0.5) * 28.0,
                size * (0.70 + 0.60 * u1) * (0.85 + 0.15 * math.sin(math.pi * p)),
                1.0 - p * math.sqrt(p),
            ))
        elif kind in ("flame", "flame_alt"):
            # 火焰 v3（2026-10-18 用户口径：v2 仍不像「酷炫燃系」）：
            # 逐粒子独立寿命 + 错峰出生形成连续火流；**加速上腾**
            # （p^1.35，浮力口径，不是 v2 的减速 p^0.85）；双频湍流横摆；
            # 尺寸「快涨后收」（Aeg 模板 \fscx 60→240→40 的缩放包络）；
            # 高频闪烁 + 长灭；**速度拉伸**——越升越窄越长，读作火舌
            # 而不是同尺寸贴纸。出生点压在字框下半部（燃烧面在字身/字底）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            birth = u3 * life * 0.30
            # 每颗独立寿命（0.62~1.0 窗），但必须在本 burst 窗内自然播完。
            life_i = min(
                life * (0.62 + 0.38 * u5),
                max(life - birth, 1.0),
            )
            p = (tau - birth) / life_i
            if p <= 0.0 or p >= 1.0:
                continue
            rise = travel * p ** 1.35
            sway = (
                math.sin((u2 + p * 1.7) * 2.0 * math.pi) * 0.30
                + math.sin((u4 * 0.7 + p * 3.3) * 2.0 * math.pi) * 0.12
            ) * size * (0.35 + 0.65 * p)
            grow = 1.0 - (1.0 - min(p / 0.16, 1.0)) ** 2
            cool = 1.0 - 0.50 * max((p - 0.50) / 0.50, 0.0) ** 1.3
            flicker = 0.70 + 0.30 * math.sin(
                (p * 5.5 + u4 * 3.0) * 2.0 * math.pi
            )
            alpha = min(p / 0.08, 1.0) * (1.0 - p) ** 0.45 * flicker
            lean = (
                math.cos((u2 + p * 1.7) * 2.0 * math.pi) * 10.0
                + (u4 - 0.5) * 8.0
            )
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.55 + sway,
                origin_y + (0.12 + 0.30 * u2) * box_h - rise,
                lean,
                size * (0.45 + 0.65 * grow) * cool * (0.85 + 0.30 * u1),
                alpha,
                1.0 - 0.25 * p,
                1.0 + 0.95 * p,
            ))
        elif kind == "flame_glow":
            # 火焰背光（flame_glow，flame 的伴生内部 kind）：字后一团大尺寸
            # 低透明度火形，慢升、缓闪、几乎不缩——给整行火“照亮背景”的
            # 氛围层（画在文字之下，不抢正文清晰度）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            birth = u3 * life * 0.25
            life_i = min(
                life * (0.70 + 0.30 * u4),
                max(life - birth, 1.0),
            )
            p = (tau - birth) / life_i
            if p <= 0.0 or p >= 1.0:
                continue
            rise = travel * p ** 1.05
            sway = (
                math.sin((u2 + p * 1.2) * 2.0 * math.pi) * size * 0.18
            )
            glow_pulse = 0.72 + 0.28 * math.sin(
                (p * 2.5 + u2) * 2.0 * math.pi
            )
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.70 + sway,
                origin_y + (0.05 + 0.25 * u2) * box_h - rise,
                (u4 - 0.5) * 10.0,
                size * (0.82 + 0.18 * math.sin(math.pi * p))
                * (0.85 + 0.30 * u1),
                min(p / 0.15, 1.0) * (1.0 - p) ** 1.2 * glow_pulse * 0.18,
                1.0,
                1.0 + 0.25 * p,
            ))
        elif kind in ("lightning", "lightning_glow"):
            # 闪电（2026-10，AE Advanced Lightning 参考）：每颗粒子是一次
            # 极短放电（90~220ms），在 burst 窗口内随机时刻重击；几乎不
            # 位移、只做小角度倾斜，alpha 走「快速点亮 → 高频频闪 →
            # 熄灭」。核心（lightning）画在文字前；同轨迹放大副本
            # （lightning_glow）画在文字后，低透明度当辉光。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            birth = u3 * life * 0.72
            life_i = min(
                100.0 + 140.0 * u4,
                max(life - birth, 1.0),
            )
            p = (tau - birth) / life_i
            if p <= 0.0 or p >= 1.0:
                continue
            # 频闪：2~3 个亮脉冲（AE 里 conductivity 抖动造成的重燃感）。
            strobe = 0.55 + 0.45 * abs(
                math.sin((p * 2.6 + u5 * 2.0) * math.pi)
            )
            alpha = min(p / 0.08, 1.0) * (1.0 - p) ** 0.65 * strobe
            if kind == "lightning_glow":
                alpha *= LIGHTNING_GLOW_ALPHA
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 1.10,
                origin_y - box_h * 0.08 + (u2 - 0.5) * box_h * 0.18,
                (u4 - 0.5) * 14.0,
                size * (0.90 + 0.10 * math.sin(math.pi * p)),
                alpha,
            ))
        elif kind == "ember":
            # 余烬火星（ember，flame 伴生内部 kind）：小圆点快速上蹿，
            # 受“重力”减速（1-(1-p)^2），带横向漂移 + 双频摆动 + 高频
            # 闪烁（火星忽明忽暗）；与下坠的灰烬 ash 一升一落。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            birth = u3 * life * 0.55
            life_i = min(
                life * (0.50 + 0.50 * u5),
                max(life - birth, 1.0),
            )
            p = (tau - birth) / life_i
            if p <= 0.0 or p >= 1.0:
                continue
            rise = travel * (1.0 - (1.0 - p) ** 2)
            drift = (u1 - 0.5) * travel * 0.55 * p
            sway = (
                math.sin((u2 + p * 2.2) * 2.0 * math.pi) * size * 1.3
            )
            twinkle = 0.55 + 0.45 * math.sin(
                (p * 9.0 + u2 * 4.0) * 2.0 * math.pi
            )
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.75 + drift + sway,
                origin_y + (0.10 + 0.30 * u4) * box_h - rise,
                p * 360.0 * (u4 - 0.5),
                size * (0.55 + 0.55 * u1) * (1.0 - 0.55 * p),
                min(p / 0.08, 1.0) * (1.0 - p) ** 0.9 * twinkle,
            ))
        elif kind == "ash":
            # 灰烬（flame 退场伴生）：自字框出生后**飘落**（灰烬下坠，
            # 区别于火焰上升）+ 小幅摇摆 + 边落边缩小熄灭 + 慢自旋。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            delay = u3 * life * 0.20
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            fall = (0.35 + 0.45 * u2) * box_h * p
            sway = math.sin((u2 + p * 1.2) * 2.0 * math.pi) * size * 0.80
            out.append(ParticleDraw(
                origin_x + (u1 - 0.5) * box_w * 0.8 + sway,
                origin_y + (u2 - 0.3) * box_h * 0.4 + fall,
                (u4 - 0.5) * 120.0 + p * (u3 - 0.5) * 180.0,
                size * (0.5 + 0.6 * u1) * (1.0 - 0.45 * p),
                min(p * 5.0, 1.0) * (1.0 - p) ** 1.15,
            ))
        elif kind == "petal":
            # 花瓣飘落（Aegisub「\moves4 四点飘移」等效运动学）：sin 横向
            # 摇摆 + 单调竖直行程 + 全程连续翻转。位置寻路与星光/音符同
            # 约定——偏移全部取 box_w/box_h 比例（星光 star_y_fraction 的
            # 「只稍微溢出字形顶」口径；音符出生在 -0.35 box_h、行程按
            # 粒子尺寸折算），不用字号倍数，避免飘进行间距/上一行
            #（2026-10 用户口径：初版 travel=1.6em 出生过高）。
            # sweep>0 飘入 ease-out 汇拢、sweep<0 飘散 smoothstep 离场、
            # sweep==0 唱字飘动小行程缓起缓落。随机出生延迟错峰
            # （快进慢出包络，\fad 同款）。镜像 d2d_fx.cpp petal 分支。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            delay = u3 * 110.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            sway = (
                math.sin((u2 + p * (0.9 + 0.9 * u5)) * 2.0 * math.pi)
                * size * (0.8 + 0.7 * u4)
            )
            size_i = size * (0.72 + 0.55 * u4)
            spin = (
                u3 * 360.0
                + p * (160.0 + 240.0 * u5) * (1.0 if u4 >= 0.5 else -1.0)
            )
            if sweep > 0:
                # 花瓣飘入：自字形顶上方 ease-out 减速飘落，落点压到字底
                # （0.42~0.58 box_h，2026-10-18：至少到主文字最底部才消失）；
                # 透明度前 75% 保持，最后一段才收。
                eased = 1.0 - (1.0 - p) * (1.0 - p)
                land_x = (u1 - 0.5) * box_w * 0.85
                land_y = (0.42 + 0.16 * u4) * box_h
                start_x = land_x + (u5 - 0.5) * box_w * 0.7
                start_y = -(0.60 + 0.30 * u2) * box_h
                hold_fade = (
                    1.0
                    if p <= PETAL_FADE_START
                    else max(
                        0.0,
                        (1.0 - p) / (1.0 - PETAL_FADE_START),
                    ) ** 1.2
                )
                out.append(ParticleDraw(
                    origin_x + start_x + (land_x - start_x) * eased + sway,
                    origin_y + start_y + (land_y - start_y) * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 4.0, 1.0) * hold_fade,
                ))
            elif sweep < 0:
                # 花瓣飘散：横向飘逸保持随机，终点向右；纵向落到字底下方
                # （0.55~0.85 box_h），透明度同样前 75% 保持、落到底才收。
                eased = p * p * (3.0 - 2.0 * p)
                start_x = (u1 - 0.5) * box_w * 0.85
                end_x = start_x + (0.5 + 0.9 * u5) * box_w
                start_y = (u4 - 0.5) * box_h * 0.55
                hold_fade = (
                    1.0
                    if p <= PETAL_FADE_START
                    else max(
                        0.0,
                        (1.0 - p) / (1.0 - PETAL_FADE_START),
                    ) ** 1.2
                )
                out.append(ParticleDraw(
                    origin_x + start_x + (end_x - start_x) * eased + sway,
                    origin_y + start_y + (0.55 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.90 + 0.10 * (1.0 - p)),
                    min(p * 4.0, 1.0) * hold_fade,
                ))
            else:
                # 花瓣飘动：字框上半出生（星光的偏上口径），小行程缓沉，
                # sin 包络淡入淡出。
                eased = p * p * (3.0 - 2.0 * p)
                out.append(ParticleDraw(
                    origin_x + (u1 - 0.5) * box_w * 0.7 + sway,
                    origin_y - (0.25 + 0.30 * u4) * box_h
                    + (0.40 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.85 + 0.15 * math.sin(math.pi * p)),
                    min(p * 5.0, 1.0) * math.sin(math.pi * p),
                ))
        elif kind == "feather":
            # 羽毛（2026-10，AE 漂浮表达式口径）：`x = sin(t*speed)*amp` 的
            # 钟摆横摆 + 缓慢下落 + 连续自转；额外用 stretch_x 做 0.55~1.0
            # 的周期性压扁（羽片侧对镜头），是羽毛区别于花瓣的关键观感。
            # sweep>0 飘入（自字顶上方减速落到字底）、sweep<0 飘散（落到
            # 字底下方并向右飘）、sweep==0 唱字（字框上半小行程缓沉）。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            delay = u3 * 140.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            # 钟摆横摆：低频主摆 + 相位随机的倍频，幅度比花瓣大（羽毛轻、
            # 受风面积大）。位置全部取 box_w/box_h 比例（与花瓣同约定）。
            sway = math.sin(
                (u2 + p * (0.55 + 0.50 * u5)) * 2.0 * math.pi
            ) * size * (1.00 + 0.80 * u4)
            size_i = size * (0.85 + 0.40 * u4)
            spin = (
                u3 * 360.0
                + p * (120.0 + 240.0 * u5) * (1.0 if u4 >= 0.5 else -1.0)
            )
            # 羽片翻转：横向压扁到 0.55~1.0（侧对镜头时最窄），周期 1.5 次。
            flutter = 0.55 + 0.45 * abs(
                math.cos((p * 1.5 + u5) * math.pi)
            )
            hold_fade = (
                1.0
                if p <= FEATHER_FADE_START
                else max(
                    0.0,
                    (1.0 - p) / (1.0 - FEATHER_FADE_START),
                ) ** 1.2
            )
            if sweep > 0:
                # 羽毛飘入：ease-out 减速降落，落点压到字底（0.42~0.60
                # box_h），从字顶上方 0.75~1.05 box_h 处起飘。
                eased = 1.0 - (1.0 - p) * (1.0 - p)
                land_x = (u1 - 0.5) * box_w * 0.90
                land_y = (0.42 + 0.18 * u4) * box_h
                start_x = land_x + (u5 - 0.5) * box_w * 0.80
                start_y = -(0.75 + 0.30 * u2) * box_h
                out.append(ParticleDraw(
                    origin_x + start_x + (land_x - start_x) * eased + sway,
                    origin_y + start_y + (land_y - start_y) * eased,
                    spin,
                    size_i * (0.90 + 0.10 * math.sin(math.pi * p)),
                    min(p * 4.0, 1.0) * hold_fade,
                    flutter,
                    1.0,
                ))
            elif sweep < 0:
                # 羽毛飘散：自字框剥落，向右下方大行程飘走（smoothstep，
                # 落点 0.55~0.85 box_h），前 80% 不透明度保持。
                eased = p * p * (3.0 - 2.0 * p)
                start_x = (u1 - 0.5) * box_w * 0.90
                end_x = start_x + (0.25 + 0.70 * u5) * box_w
                start_y = (u4 - 0.5) * box_h * 0.45
                out.append(ParticleDraw(
                    origin_x + start_x + (end_x - start_x) * eased + sway,
                    origin_y + start_y
                    + (0.55 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.90 + 0.10 * (1.0 - p)),
                    min(p * 4.0, 1.0) * hold_fade,
                    flutter,
                    1.0,
                ))
            else:
                # 羽毛飘动：字框上半出生，缓沉小行程，sin 包络淡入淡出。
                eased = p * p * (3.0 - 2.0 * p)
                out.append(ParticleDraw(
                    origin_x + (u1 - 0.5) * box_w * 0.75 + sway,
                    origin_y - (0.30 + 0.30 * u4) * box_h
                    + (0.45 + 0.30 * u2) * box_h * eased,
                    spin,
                    size_i * (0.88 + 0.12 * math.sin(math.pi * p)),
                    min(p * 5.0, 1.0) * math.sin(math.pi * p),
                    flutter,
                    1.0,
                ))
        elif kind in SNOW_KINDS:
            # 雪花飘落（AE CC Snowfall 运动签名 + 2026-10 用户口径）：
            # 出生高度统一锚在 **ruby 盒中心-底部二分之一处**（主文字顶
            # 上方 ≈0.68~0.90 box_h 窄随机带）；下沉 = 匀速（终端速度感，
            # eased=线性）+ 宽幅低频摇摆 + 慢自转；**尺寸随播放由大变小**。
            # 出入场落点压到字底（≥0.48 box_h），透明度前 78% 保持不透明、
            # 最后 22% 才收——旧版 p=0.5 已 α≈0.4，像半途消失。
            # sweep>0 飘入、sweep<0 飘散、sweep==0 唱字飘动。
            # 镜像 d2d_fx.cpp snow 分支。
            u1 = fx_unit_hash(seed + i, 1)
            u2 = fx_unit_hash(seed + i, 2)
            u3 = fx_unit_hash(seed + i, 3)
            u4 = fx_unit_hash(seed + i, 4)
            u5 = fx_unit_hash(seed + i, 5)
            delay = u3 * 160.0
            p = min(max((tau - delay) / max(life - delay, 1.0), 0.0), 1.0)
            if p <= 0.0:
                continue
            sway = (
                math.sin((u2 + p * (0.35 + 0.45 * u5)) * 2.0 * math.pi)
                * size * (0.75 + 0.65 * u4)
            )
            size_i = size * (0.70 + 0.50 * u4) * (1.05 - 0.40 * p)
            spin = (
                u3 * 360.0
                + p * (80.0 + 140.0 * u5) * (1.0 if u4 >= 0.5 else -1.0)
            )
            # 出生锚：ruby 盒中心-底部二分之一处（字形顶 -0.5 之上）。
            birth_y = -(0.68 + 0.22 * u2) * box_h
            eased = p  # 匀速下沉（雪的终端速度感）
            hold_fade = (
                1.0
                if p <= SNOW_FADE_START
                else max(
                    0.0,
                    (1.0 - p) / (1.0 - SNOW_FADE_START),
                ) ** 1.2
            )
            if sweep > 0:
                # 雪花飘入：自 ruby 锚一路落到**字底或更低**
                # （2026-10-18：至少到主文字最底部才消失）。
                land_x = (u1 - 0.5) * box_w * 0.85
                land_y = (0.48 + 0.14 * u4) * box_h
                start_x = land_x + (u5 - 0.5) * box_w * 0.6
                out.append(ParticleDraw(
                    origin_x + start_x + (land_x - start_x) * eased + sway,
                    origin_y + birth_y + (land_y - birth_y) * eased,
                    spin,
                    size_i,
                    min(p * 3.0, 1.0) * hold_fade,
                ))
            elif sweep < 0:
                # 雪花飘散：同样自 ruby 锚出生落到字底或更低，横向随机
                # 摇摆、终点向右缓漂；透明度同样落到底才收。
                land_x = (u1 - 0.5) * box_w * 0.85
                end_x = land_x + (0.35 + 0.6 * u5) * box_w
                land_y = (0.50 + 0.24 * u2) * box_h
                start_x = land_x + (u5 - 0.5) * box_w * 0.5
                out.append(ParticleDraw(
                    origin_x + start_x + (end_x - start_x) * eased + sway,
                    origin_y + birth_y + (land_y - birth_y) * eased,
                    spin,
                    size_i,
                    min(p * 3.0, 1.0) * hold_fade,
                ))
            else:
                # 雪花飘动：同样自 ruby 锚出生，匀速缓沉穿过字框，
                # sin 包络淡入淡出。
                land_y = (u4 - 0.5) * box_h * 0.6
                out.append(ParticleDraw(
                    origin_x + (u1 - 0.5) * box_w * 0.7 + sway,
                    origin_y + birth_y + (land_y - birth_y) * eased,
                    spin,
                    size_i,
                    min(p * 4.0, 1.0) * math.sin(math.pi * p),
                ))
    return out
