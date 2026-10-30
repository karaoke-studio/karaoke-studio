#include "d2d_fx.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace krok::subtitle::native::direct2d {
namespace {

// 幅度参数（与 transitions.py 顶部常量镜像）。
constexpr float kTrackingSpreadEm = 1.6f;
constexpr float kWaveAmplitudeEm = 0.5f;
constexpr float kWaveAmplitudeMinPx = 24.0f;
constexpr float kScatterTravelEm = 2.2f;
constexpr float kScatterShrink = 0.4f;
constexpr float kConvergeShrink = 0.2f;
// 编排与 Python char_fade 完全同构（镜像 transitions.CHAR_FADE_* 常量）：
// 350ms 内逐字错峰、每字 250ms 走完行程；激活门 = entry/exitDurationMs>0
//（专门的播放时间，窗口判定同 char_fade 的 600ms）。
constexpr int kCharFadeIntroDelayMs = 350;
constexpr int kCharFadeInOutTimeMs = 250;
// 羽毛/闪电（镜像 particles.py FEATHER_FADE_START /
// LIGHTNING_GLOW_ALPHA）。
constexpr float kFeatherFadeStart = 0.85f;
constexpr float kLightningGlowAlpha = 0.22f;
constexpr float kLightningGlowSizeScale = 1.35f;
// 汇聚式下落（镜像 particles._fall_progress）：三次 Hermite，
// 初速 0.2、接近落点减速到 0；末段透明度随速度渐隐。
constexpr float kFallInitialV = 0.20f;
constexpr float kFallStartMin = 0.55f;
constexpr float kFallStartSpan = 0.45f;
constexpr float kFallSpeedFadeStart = 0.55f;
inline float fallProgress(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    return (kFallInitialV - 2.0f) * v * v * v
        + (3.0f - 2.0f * kFallInitialV) * v * v
        + kFallInitialV * v;
}
inline float fallVelocity(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    return 3.0f * (kFallInitialV - 2.0f) * v * v
        + 2.0f * (3.0f - 2.0f * kFallInitialV) * v
        + kFallInitialV;
}
inline float fallSpeedFade(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    if (v <= kFallSpeedFadeStart) {
        return 1.0f;
    }
    const float speed = fallVelocity(v) / 1.45f;
    float q = std::clamp(speed / 0.55f, 0.0f, 1.0f);
    return q * q * (3.0f - 2.0f * q);
}
// 走字风（镜像 particles.py）：下落粒子向右后方漂 + 固定后倾角。
constexpr float kPetalWindFraction = 0.55f;
constexpr float kSnowWindFraction = 0.45f;
constexpr float kFeatherWindFraction = 0.65f;
constexpr float kFallLeanDeg = -22.0f;
constexpr float kSnowLeanDeg = -30.0f;
constexpr float kFeatherLeanDeg = -32.0f;
// AE Box emitter + 连续出生（Particular 雪/花瓣教程）：出入场每颗在
// 0~45% 寿命内错峰出生，晚出生的从更低处开始；唱字档只做小幅错峰。
constexpr float kFallSpawnSpread = 0.45f;
constexpr float kFallSpawnSpreadSing = 0.18f;
// AE 下落特效景深（Particular 多层 far/mid/near + 随机 size）：每颗
// 用 u4 固定 0.5~1.6× 的深度尺寸，远小近大。
constexpr float kPetalSizeMin = 0.45f;
constexpr float kPetalSizeSpan = 1.20f;
constexpr float kSnowSizeMin = 0.40f;
constexpr float kSnowSizeSpan = 0.88f;
constexpr float kFeatherSizeMin = 0.45f;
constexpr float kFeatherSizeSpan = 1.20f;
// 音符/爱心四档尺寸景深（镜像 particles.PARTICLE_DEPTH_TIERS）：按
// (i + seed>>5) 轮换到不同粒子，与横向出生点解耦（2026-10-22 经验 §3.5）。
constexpr float kParticleDepthTiers[] = {
    0.72f, 0.92f, 1.12f, 1.34f,
};
// 扫字线特效四档尺寸（镜像 particles.SCANLINE_FX_DEPTH_TIERS）：与上面
// 音符/爱心那套既有档位刻意分开——两端旧档值不同，新档统一取这一份。
constexpr float kScanlineDepthTiers[] = {
    0.60f, 0.85f, 1.15f, 1.45f,
};
// 落雷簇状重击时刻（镜像 particles.LIGHTNING_STRIKE_BIRTHS /
// LIGHTNING_STRIKE_JITTER）：真实闪电两三连击 + 停顿 + 补击。
constexpr float kLightningStrikeBirths[] = {
    0.00f, 0.03f, 0.10f, 0.15f, 0.38f, 0.42f, 0.51f, 0.69f, 0.73f,
};
constexpr float kLightningStrikeJitter = 0.035f;

float clampedRatio(int elapsedMs, int durationMs) {
    if (durationMs <= 0) {
        return 1.0f;
    }
    const float value = static_cast<float>(elapsedMs)
        / static_cast<float>(durationMs);
    return std::clamp(value, 0.0f, 1.0f);
}

float centerNorm(int index, int count) {
    if (count <= 1) {
        return 0.0f;
    }
    const float half = static_cast<float>(count - 1) / 2.0f;
    return (static_cast<float>(index) - half) / half;
}

}  // namespace

float fxUnitHash(std::uint32_t index, std::uint32_t salt) {
    std::uint32_t h = index * 0x85EBCA77u + salt * 0xC2B2AE3Du;
    h ^= h >> 15;
    h *= 0x2545F491u;
    h ^= h >> 13;
    return static_cast<float>(h & 0xFFFFFFu) / 16777216.0f;
}

// 星光族（sparkle/twinkle）纵向锚点偏置 + 连续象限去重——镜像
// particles.star_y_fraction / star_y_resample（2026-10 用户口径：尽量多
// 出现在主文字上侧，锚点只稍微溢出字形顶 ≈12% 行高，连续两颗不落同
// 象限）。
constexpr float kStarYTop = -0.62f;
constexpr float kStarYSpan = 0.92f;
constexpr float kStarYSplitU = 0.62f / 0.92f;

inline float starYFraction(float u) {
    return kStarYTop + kStarYSpan * u;
}

inline int starQuadrant(bool left, bool top) {
    return (left ? 2 : 0) + (top ? 1 : 0);
}

inline float starYResample(
    float u2, float alt6, float alt7, int prevQ, bool left
) {
    if (starQuadrant(left, starYFraction(u2) < 0.0f) != prevQ) {
        return u2;
    }
    if (starQuadrant(left, starYFraction(alt6) < 0.0f) != prevQ) {
        return alt6;
    }
    if (starQuadrant(left, starYFraction(alt7) < 0.0f) != prevQ) {
        return alt7;
    }
    if (prevQ & 1) {
        // 上一颗在上 → 取下半段（frac ∈ [0, +0.30]，必为下侧）。
        return kStarYSplitU + (1.0f - kStarYSplitU) * alt6;
    }
    // 上一颗在下 → 取上半段（frac ∈ [-0.62, 0)，必为上侧）。
    return kStarYSplitU * alt7;
}

int fallEntryLeadMs(const std::string &effect) {
    // 镜像 particles.fall_entry_lead_ms：入场粒子提前 20% 粒子寿命发射。
    if (effect == "petal" || effect == "feather") {
        return 340;
    }
    if (effect == "snow" || effect == "snow_solid") {
        return 300;
    }
    return 0;
}

GeoCharState geoCharState(
    const std::string &effect,
    float fontPx,
    int index,
    int count,
    int tMs,
    int startMs,
    int durationMs,
    bool exitPhase,
    float charCenterX,
    float lineCenterX,
    bool hasCenters
) {
    GeoCharState state;
    const float font = std::max(fontPx, 1.0f);
    // 编排总时长 = 「入场/退场动画时长」旋钮（调用方门保证 >0）；
    // 错峰:行程保持 7:5（默认 600ms 时即 350/250）。
    const int totalMs = std::clamp(durationMs, 120, 3000);
    const int staggerMs = totalMs * 7 / 12;
    const int travelMs = std::max(totalMs - staggerMs, 40);
    const auto staggerProgress = [&]() {
        const int step = count <= 1 ? 0 : staggerMs / (count - 1);
        return clampedRatio(tMs - startMs - step * index, travelMs);
    }();
    // 入场透明度 4 倍速上升 + smoothstep 运动包络：字符在行程前 1/4 即
    // 全不透明，位移仍有 8 成以上，保证「先清晰可见、再走完行程」。
    const auto smoothstep = [](float p) {
        return p * p * (3.0f - 2.0f * p);
    };

    if (effect == "tracking_in") {
        const float p = staggerProgress;
        const float q = smoothstep(p);
        const float spread = font * kTrackingSpreadEm;
        state.dx = centerNorm(index, count) * spread * (1.0f - q);
        state.opacity = std::min(p * 4.0f, 1.0f);
        return state;
    }
    if (effect == "wave_in") {
        const float p = staggerProgress;
        const float q = smoothstep(p);
        const float amplitude = std::max(font * kWaveAmplitudeEm, kWaveAmplitudeMinPx);
        state.dy = amplitude * (1.0f - q);
        state.opacity = std::min(p * 4.0f, 1.0f);
        return state;
    }
    if (effect == "scatter_out") {
        const float p = staggerProgress;
        const double theta = static_cast<double>(
            fxUnitHash(static_cast<std::uint32_t>(index), 1u)
        ) * 2.0 * 3.14159265358979323846;
        const float speed = 0.55f + 0.45f * fxUnitHash(
            static_cast<std::uint32_t>(index), 2u
        );
        const float spin = (
            fxUnitHash(static_cast<std::uint32_t>(index), 3u) - 0.5f
        ) * 720.0f;
        const float travel = font * kScatterTravelEm * speed
            * (1.0f - (1.0f - p) * (1.0f - p));
        const float scale = 1.0f - kScatterShrink * p;
        state.opacity = (1.0f - p) * (1.0f - p);
        state.dx = static_cast<float>(std::cos(theta)) * travel;
        state.dy = static_cast<float>(std::sin(theta)) * travel;
        state.rotation = spin * p;
        state.scaleX = scale;
        state.scaleY = scale;
        return state;
    }
    if (effect == "stretch_in") {
        // 逐字拉伸入场（光条凝聚，Aegisub \fscx+\blur+\t 同款语言）。
        const float p = staggerProgress;
        state.opacity = std::min(p * 4.0f, 1.0f);
        const float q = smoothstep(p);
        state.glowAlpha = (1.0f - q) * std::sqrt(1.0f - q);
        state.glowRadiusEm = 1.2f - 0.5f * q;
        state.scaleX = 1.0f + 2.2f * (1.0f - q);
        return state;
    }
    if (effect == "stretch_out") {
        // 逐字拉伸退场：反向——清晰字形横向拉伸成光条弥散，同时淡出。
        const float p = staggerProgress;
        const float q = smoothstep(p);
        state.opacity = 1.0f - q;
        constexpr float pi = 3.14159265358979323846f;
        state.glowAlpha = std::sin(pi * q) * 0.9f;
        state.glowRadiusEm = 0.7f + 0.8f * q;
        state.scaleX = 1.0f + 2.2f * q;
        return state;
    }
    if (effect == "glow_in" || effect == "glow_out") {
        // 辉光浮现/消散：整行同步（无逐字错峰）横向拉伸 + 高强度
        // 大半径光晕（逐字拉伸的整行版；曲线与 Python 镜像）。
        const float p = clampedRatio(tMs - startMs, travelMs);
        const float q = smoothstep(p);
        if (effect == "glow_in") {
            state.opacity = std::min(p * 3.0f, 1.0f);
            state.glowAlpha = (1.0f - q) * std::sqrt(1.0f - q);
            state.glowRadiusEm = 1.0f - 0.25f * q;
            state.scaleX = 1.0f + 0.6f * (1.0f - q);
        } else {
            state.opacity = 1.0f - q;
            constexpr float pi = 3.14159265358979323846f;
            state.glowAlpha = std::sin(pi * q);
            state.glowRadiusEm = 1.0f + 0.9f * q;
            state.scaleX = 1.0f + 0.6f * q;
        }
        return state;
    }
    if (effect == "flame") {
        // 火焰档文字本体（镜像 transitions.py flame 分支）：参考既有
        // stretch_in/out 的光条凝聚/弥散语言，但延后到火烧到后段（62%）
        // 才发生——入场前 62% 隐形，后 38% 由 3.2× 横向光条收束成字形；
        // 退场前 62% 清晰，后 38% 横向拉伸成光条淡出。不叠 stretch 的
        // 辉光层（火焰粒子本身已是高亮层）。
        const float p = staggerProgress;
        const float raw = std::clamp((p - 0.62f) / 0.38f, 0.0f, 1.0f);
        const float q = smoothstep(raw);
        if (exitPhase) {
            state.opacity = 1.0f - q;
            state.scaleX = 1.0f + 2.2f * q;
        } else {
            state.opacity = std::min(raw * 4.0f, 1.0f);
            state.scaleX = 1.0f + 2.2f * (1.0f - q);
        }
        return state;
    }
    if (effect == "sparkle" || effect == "ripple" || effect == "note"
        || effect == "heart" || effect == "petal"
        || effect == "snow" || effect == "snow_solid"
        || effect == "feather" || effect == "lightning") {
        // 粒子类出入场动画的本体：文字逐字显形（入场 4 倍速透明度，
        // 粒子拼接同款编排），退场逐字淡出；粒子由 planner 叠加。
        const float p = staggerProgress;
        if (exitPhase) {
            const float q = smoothstep(p);
            state.opacity = 1.0f - q;
        } else {
            state.opacity = std::min(p * 4.0f, 1.0f);
        }
        return state;
    }
    if (effect == "assemble_in") {
        // 粒子拼接：粒子飞入期间字形未成形，抵达后显形。
        const float p = staggerProgress;
        const float appear = smoothstep(p);
        state.opacity = std::max(0.0f, (appear - 0.45f) / 0.55f);
        return state;
    }
    if (effect == "dissolve_out") {
        // 粒子消散：字形先行散去，粒子随后飞离。
        const float p = staggerProgress;
        const float vanish = std::clamp(p / 0.55f, 0.0f, 1.0f);
        state.opacity = 1.0f - vanish * vanish * (3.0f - 2.0f * vanish);
        return state;
    }
    // converge_out
    const float p = staggerProgress;
    const float eased = p * p * (3.0f - 2.0f * p);
    if (hasCenters) {
        state.dx = (lineCenterX - charCenterX) * eased;
    } else {
        const float halfWidthEm = static_cast<float>(count) * 0.5f;
        state.dx = -centerNorm(index, count) * font * halfWidthEm * eased;
    }
    const float scale = 1.0f - kConvergeShrink * eased;
    state.opacity = 1.0f - eased;
    state.scaleX = scale;
    state.scaleY = scale;
    return state;
}

D2D1_MATRIX_3X2_F geoCharMatrix(
    const GeoCharState &state,
    float centerX,
    float centerY
) {
    return D2D1::Matrix3x2F::Translation(-centerX, -centerY)
        * D2D1::Matrix3x2F::Scale(state.scaleX, state.scaleY)
        * D2D1::Matrix3x2F::Rotation(state.rotation)
        * D2D1::Matrix3x2F::Translation(
            centerX + state.dx, centerY + state.dy
        );
}

std::vector<FxParticle> burstParticlesAt(
    const ParticleBurst &burst,
    int tMs,
    float originX,
    float originY,
    float boxW,
    float boxH
) {
    std::vector<FxParticle> out;
    if (tMs < burst.startMs || tMs > burst.endMs) {
        return out;
    }
    // 粒子速度调整（2026-10，镜像 particles.burst_particles_at）：burst 窗口
    // 已按速度缩放，这里把 tau/life 映射回标准速率，让求值器内部的**绝对**
    // 发射间隔（涟漪环 150ms / 拼接 90ms / 星光抖动 130ms 等）与寿命同比
    // 缩放，密度观感不随速度突变。timeScale=1（旧 IR / 原速）时逐位等价。
    // scanline 的 emitMs 是真实锋面时间，Python 规划器不对它写 timeScale。
    const float timeScale = burst.timeScale > 0.0f ? burst.timeScale : 1.0f;
    const float life = std::max(
        static_cast<float>(burst.endMs - burst.startMs) / timeScale,
        1.0f
    );
    const float tau = static_cast<float>(tMs - burst.startMs) / timeScale;
    const float size = burst.sizePx;
    const float travel = burst.travelPx;
    // 唱字火焰按簇横向扫过的发射点偏移（镜像 particles.burst_particles_at
    // 的 emit_x；0 = 不偏移）。
    const float emitX = burst.emitX * boxW;

    if (burst.kind == "ripple") {
        // 水波纹（镜像 particles.burst_particles_at）：3 枚细描边圆环错峰
        // 扩散（第 i 环延迟 150ms），「自小圈快速扩开 → 满径时淡出」。
        const int rings = std::max(burst.count, 1);
        for (int i = 0; i < rings; ++i) {
            const float delay = static_cast<float>(i) * 150.0f;
            const float lifeI = std::max(
                static_cast<float>(life) - delay, 1.0f
            );
            const float p = std::clamp((tau - delay) / lifeI, 0.0f, 1.0f);
            if (p <= 0.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            const float scale = size * (0.25f + 0.85f * eased);
            out.push_back(FxParticle{
                originX, originY, 0.0f, scale, (1.0f - p) * (1.0f - p),
            });
        }
        return out;
    }

    const std::uint32_t seed = burst.seed;

    if (burst.kind == "scanline") {
        // 扫字线特效（2026-10）：镜像 particles.burst_particles_at 的同名
        // 分支。``emitMs`` 是锋面行进窗口（= 字符唱字窗），窗口尾部剩余
        // 部分是每颗粒子的固定寿命；出生时刻越晚 → 锋面越靠右（RTL 反
        // 过来），粒子从锋面当前位置向后方飘走，换字后旧粒子继续拖尾。
        const int window = std::max(burst.endMs - burst.startMs, 1);
        const int emitRaw = burst.emitMs > 0 ? burst.emitMs : window;
        const int emit = std::clamp(emitRaw, 1, window);
        const float tail = static_cast<float>(std::max(window - emit, 1));
        const float emitF = static_cast<float>(emit);
        const float behind = burst.reverse ? 1.0f : -1.0f;
        const int total = std::max(burst.count, 1);
        const std::string &sprite = burst.sprite;
        constexpr float pi = 3.14159265358979323846f;
        const auto smoothstep = [](float value) {
            const float t = std::clamp(value, 0.0f, 1.0f);
            return t * t * (3.0f - 2.0f * t);
        };
        const auto fadeOut = [&](float p, float start) {
            return 1.0f - smoothstep(
                (p - start) / std::max(1.0f - start, 1e-6f)
            );
        };
        const auto depthCount = static_cast<std::uint32_t>(
            sizeof(kScanlineDepthTiers) / sizeof(kScanlineDepthTiers[0])
        );
        out.reserve(static_cast<std::size_t>(total));
        for (int i = 0; i < total; ++i) {
            const std::uint32_t index = seed + static_cast<std::uint32_t>(i);
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float birth = (
                static_cast<float>(i) + 0.15f + 0.70f * u4
            ) / static_cast<float>(total) * emitF;
            const float p = (tau - birth) / tail;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            float frac = birth / emitF;
            if (burst.reverse) {
                frac = 1.0f - frac;
            }
            const float spawnX = originX + (frac - 0.5f) * boxW;
            const float spawnY = originY + (u1 - 0.5f) * boxH * 0.92f;
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            const auto depthIndex = (
                static_cast<std::uint32_t>(i) + (seed >> 5)
            ) % depthCount;
            const float baseSize = size * kScanlineDepthTiers[depthIndex]
                * (0.90f + 0.20f * u2);
            const float fadeIn = std::min(p / 0.10f, 1.0f);
            if (sprite == "petal") {
                // 樱花：与星光同向的左上飞行（2026-10 用户口径：要有走字
                // 速度感），保留花瓣自旋与小扑动；不再向下飘落。
                const float drift = behind * travel
                    * (0.45f + 0.35f * u3) * eased;
                const float rise = boxH * (0.20f + 0.40f * u2) * eased;
                const float sway = std::sin(
                    (u3 + p * (0.55f + 0.25f * u5)) * 2.0f * pi
                ) * size * (0.20f + 0.10f * u1);
                out.push_back(FxParticle{
                    spawnX + drift + sway,
                    spawnY - rise - std::abs(sway) * 0.35f,
                    u3 * 360.0f + p * (140.0f + 200.0f * u5)
                        * (u4 >= 0.5f ? 1.0f : -1.0f)
                        + kFallLeanDeg,
                    baseSize * (0.85f + 0.15f * std::sin(pi * p)),
                    fadeIn * fadeOut(p, 0.78f),
                });
            } else if (sprite == "feather") {
                const float drift = behind * travel
                    * (0.55f + 0.35f * u3) * eased;
                const float rise = boxH * (0.18f + 0.45f * u2) * eased;
                const float flutter = std::sin(
                    (u3 + p * (0.45f + 0.25f * u5)) * 2.0f * pi
                ) * size * (0.26f + 0.12f * u1);
                out.push_back(FxParticle{
                    spawnX + drift + flutter,
                    spawnY - rise - std::abs(flutter) * 0.35f,
                    u3 * 360.0f + p * (120.0f + 200.0f * u5)
                        * (u4 >= 0.5f ? 1.0f : -1.0f)
                        + kFeatherLeanDeg,
                    baseSize * (0.88f + 0.12f * std::sin(pi * p)),
                    fadeIn * fadeOut(p, 0.82f),
                });
            } else if (sprite == "snow" || sprite == "snow_solid") {
                // 雪花：同样朝左上飞；比羽毛轻，摆幅稍小、翻转更慢。
                const float drift = behind * travel
                    * (0.50f + 0.30f * u3) * eased;
                const float rise = boxH * (0.20f + 0.42f * u2) * eased;
                const float sway = std::sin(
                    (u3 + p * (0.50f + 0.22f * u5)) * 2.0f * pi
                ) * size * (0.24f + 0.10f * u1);
                out.push_back(FxParticle{
                    spawnX + drift + sway,
                    spawnY - rise - std::abs(sway) * 0.30f,
                    u3 * 360.0f + p * (100.0f + 180.0f * u5)
                        * (u4 >= 0.5f ? 1.0f : -1.0f)
                        + kSnowLeanDeg,
                    baseSize * (0.90f + 0.10f * std::sin(pi * p)),
                    fadeIn * fadeOut(p, 0.80f),
                });
            } else if (sprite == "note") {
                const float drift = behind * travel
                    * (0.35f + 0.30f * u3) * eased;
                const float rise = boxH * (0.25f + 0.45f * u2) * eased;
                const float sway = std::sin(
                    (u3 + p * 1.1f) * 2.0f * pi
                ) * size * 0.30f;
                out.push_back(FxParticle{
                    spawnX + drift + sway,
                    spawnY - rise,
                    (u4 - 0.5f) * 40.0f
                        + std::sin((u5 + p * 0.8f) * 2.0f * pi) * 8.0f,
                    baseSize * (0.85f + 0.15f * std::sin(pi * p)),
                    fadeIn * std::pow(1.0f - p, 1.1f),
                });
            } else {
                // 星光：锋面后向左上飞散 + 闪烁。
                const float drift = behind * travel
                    * (0.40f + 0.35f * u3) * eased;
                const float rise = boxH * (0.18f + 0.37f * u2) * eased;
                const float sway = std::sin(
                    (u3 + p * 0.9f) * 2.0f * pi
                ) * size * 0.20f;
                const float pulse = std::sin(
                    pi * std::min(p / 0.75f, 1.0f)
                );
                out.push_back(FxParticle{
                    spawnX + drift + sway,
                    spawnY - rise,
                    u3 * 90.0f + p * 80.0f,
                    baseSize * (0.45f + 0.65f * pulse),
                    fadeIn * std::pow(1.0f - p, 0.9f)
                        * (0.60f + 0.40f * std::sin(
                            (p * 3.0f + u5) * 2.0f * pi
                        )),
                });
            }
        }
        return out;
    }

    int starPrevQ = -1;  // 星光族象限链：-1 = 尚无上一颗
    out.reserve(static_cast<std::size_t>(std::max(burst.count, 0)));
    for (int i = 0; i < burst.count; ++i) {
        const std::uint32_t index = seed + static_cast<std::uint32_t>(i);
        if (burst.kind == "sparkle" || burst.kind == "twinkle") {
            // 整句扫过（镜像 particles.burst_particles_at sparkle 分支）；
            // 唱字星光（twinkle）2026-10 起并入同一运动学（字框内小扫过）。
            const float u1 = fxUnitHash(index, 1u);
            const bool left = (u1 - 0.5f) < 0.0f;
            const float u2 = starYResample(
                fxUnitHash(index, 2u),
                fxUnitHash(index, 6u),
                fxUnitHash(index, 7u),
                starPrevQ,
                left
            );
            const float yOff = starYFraction(u2) * boxH;
            starPrevQ = starQuadrant(left, yOff < 0.0f);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            float delay;
            if (burst.sweep > 0) {
                delay = u1 * 350.0f + u5 * 90.0f;
            } else if (burst.sweep < 0) {
                delay = (1.0f - u1) * 350.0f + u5 * 90.0f;
            } else {
                // 短窗口（如 120ms 退场）不能让随机出生延迟吞掉整组粒子：
                // 钳到「寿命 - 最短可见寿命」（镜像 particles.py 的
                // SPARKLE_BIRTH_JITTER_MS / SPARKLE_MIN_VISIBLE_MS）。
                delay = std::min(
                    u5 * 130.0f,
                    std::max(
                        static_cast<float>(life) - 40.0f, 0.0f
                    )
                );
            }
            const float denom = std::max(
                static_cast<float>(life) - delay, 1.0f
            );
            const float p = std::clamp((tau - delay) / denom, 0.0f, 1.0f);
            if (p <= 0.0f) {
                continue;
            }
            const float sizeI = size * (0.65f + 0.70f * u3);
            const float sizeF = sizeI * (0.15f + 0.30f * u4);
            const float scale = sizeI + (sizeF - sizeI) * p;
            const float driftX = (u4 - 0.5f) * travel * 0.35f * p;
            const float driftY = -(0.30f + 0.70f * u2) * travel * 0.45f * p;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW + driftX,
                originY + yOff + driftY,
                u3 * 90.0f + 60.0f * p,
                scale,
                1.0f - p,
            });
        } else if (burst.kind == "twinkle_classic") {
            // 旧版唱字星光（2026-10 运动学改造前的形态，逐字复刻自被
            // 2775dc99 删除的 twinkle 分支；镜像 particles.py 同名分支）：
            // 位置对称铺满整个字框、随机固定旋转、随机寿命（300–450ms）
            // 与峰值大小，sin 包络「出生→放大→熄灭」的原地闪烁。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float lifeI = 300.0f + 150.0f * u4;
            const float spread = std::max(
                static_cast<float>(life) - lifeI, 0.0f
            );
            const float delay = u3 * spread * 0.8f;
            const float p = std::clamp((tau - delay) / lifeI, 0.0f, 1.0f);
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float envelope = std::sin(pi * p);
            const float peak = size * (0.65f + 0.70f * u4);
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.95f,
                originY + (u2 - 0.5f) * boxH * 0.85f,
                u3 * 120.0f - 30.0f,
                peak * (0.30f + 0.70f * envelope),
                envelope,
            });
        } else if (burst.kind == "assemble" || burst.kind == "dissolve") {
            // 粒子拼接/消散（镜像 particles.burst_particles_at）：
            // 拼接=自随机方向远端飞向字形内随机落点后缩小熄灭；
            // 消散=自字形内随机起点飞向随机方向远端并淡出。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float delay = burst.kind == "assemble"
                ? u3 * 90.0f : u3 * 70.0f;
            const float p = std::clamp(
                (tau - delay) / std::max(
                    static_cast<float>(life) - delay, 1.0f
                ),
                0.0f, 1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            if (burst.kind == "assemble" && p >= 1.0f) {
                continue;
            }
            const float eased = p * p * (3.0f - 2.0f * p);
            constexpr float pi = 3.14159265358979323846f;
            const float theta = u2 * 2.0f * pi;
            const float speed = 0.6f + 0.5f * u4;
            const float landX = (u1 - 0.5f) * boxW * 0.9f;
            const float landY = (u4 - 0.5f) * boxH * 0.8f;
            if (burst.kind == "assemble") {
                const float startX = landX + std::cos(theta) * travel * speed;
                const float startY = landY
                    + std::sin(theta) * travel * speed * 0.7f;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * eased,
                    originY + startY + (landY - startY) * eased,
                    (u3 - 0.5f) * 240.0f * (1.0f - eased),
                    size * (0.9f - 0.35f * eased),
                    std::min(p * 6.0f, 1.0f) * (1.0f - p * p),
                });
            } else {
                out.push_back(FxParticle{
                    originX + landX
                        + std::cos(theta) * travel * speed * eased,
                    originY + landY
                        + std::sin(theta) * travel * speed * 0.7f * eased,
                    (u3 - 0.5f) * 240.0f * eased,
                    size * (0.75f + 0.25f * (1.0f - eased)),
                    (1.0f - p) * (1.0f - p * 0.5f),
                });
            }
        } else if (burst.kind == "note" || burst.kind == "heart") {
            // 音符/爱心（镜像 particles.burst_particles_at，2026-10-22 按
            // 「粒子特效实现经验」强化随机性）：首颗粒子立即出生、其余
            // 0~42% 寿命错峰；逐颗行程/出生高度/摆幅频率独立哈希；尺寸走
            // 四档景深表（与横向出生点解耦，最大那颗不再总在同一侧）。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float u6 = fxUnitHash(index, 6u);
            constexpr float pi = 3.14159265358979323846f;
            const float spawn = i == 0 ? 0.0f : u3;
            const float delay = spawn * static_cast<float>(life) * 0.42f;
            const float p = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            const float rise = travel * (0.72f + 0.55f * u4)
                * (1.0f - (1.0f - p) * (1.0f - p));
            const float swayAmp = size * (0.22f + 0.30f * u5);
            const float sway = std::sin(
                (u2 + p * (1.05f + 0.55f * u6)) * 2.0f * pi
            ) * swayAmp * (0.55f + 0.45f * p);
            const float drift = (u6 - 0.5f) * travel * 0.18f * p;
            const auto depthCount = static_cast<std::uint32_t>(
                sizeof(kParticleDepthTiers) / sizeof(kParticleDepthTiers[0])
            );
            // 无符号取模：seed 接近 2^32 时 (int) 强转会变负数、下标越界。
            const auto depthIndex = (
                static_cast<std::uint32_t>(i) + (burst.seed >> 5)
            ) % depthCount;
            const float depth = kParticleDepthTiers[depthIndex]
                * (0.94f + 0.12f * u6);
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.9f + sway + drift,
                originY - boxH * (0.24f + 0.16f * u4) - rise,
                (u2 - 0.5f) * 44.0f
                    + std::sin((u5 + p * 0.9f) * 2.0f * pi) * 9.0f,
                size * depth * (0.88f + 0.12f * std::sin(pi * p)),
                1.0f - p * std::sqrt(p),
            });
        } else if (burst.kind == "flame" || burst.kind == "flame_alt") {
            // 火焰 v3（镜像 particles.burst_particles_at flame 分支）：
            // 独立寿命 + 错峰出生形成连续火流；加速上腾 p^1.35（浮力）+
            // 双频湍流横摆；尺寸快涨后收（Aeg \fscx 60→240→40 包络）；
            // 高频闪烁 + 长灭；越升越窄越长（stretchX/Y 速度拉伸）。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float u6 = fxUnitHash(index, 6u);
            constexpr float pi = 3.14159265358979323846f;
            // 唱字档（sweep==2）：更碎寿命 + 更宽尺寸差 + 第三层湍流 +
            // 擦字反方向拖尾（2026-10-22 AE 火焰预置口径，只影响走字）。
            const bool singMode = burst.sweep == 2;
            const float birth = u3 * life * (singMode ? 0.28f : 0.34f);
            const float lifeI = std::min(
                life * (
                    singMode ? (0.45f + 0.55f * u5)
                             : (0.62f + 0.38f * u5)
                ),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * std::pow(p, 1.35f);
            float turbulence = (
                std::sin((u2 + p * 1.7f) * 2.0f * pi) * 0.45f
                + std::sin((u4 * 0.7f + p * 3.3f) * 2.0f * pi) * 0.18f
            );
            if (singMode) {
                turbulence += std::sin((u6 + p * 5.1f) * 2.0f * pi) * 0.12f;
            }
            const float sway = turbulence * size
                * (singMode ? (0.42f + 0.85f * p) : (0.35f + 0.65f * p));
            const float grow = 1.0f - std::pow(
                1.0f - std::min(p / 0.16f, 1.0f), 2.0f
            );
            // std::max(..., 0) 避免负数进入 pow；p>0.5 后才开始“冷却收缩”。
            const float cool = 1.0f - 0.50f * std::pow(
                std::max((p - 0.50f) / 0.50f, 0.0f), 1.3f
            );
            const float flicker = 0.70f + 0.30f * std::sin(
                ((singMode ? 7.5f : 5.5f) * p + u4 * 3.0f)
                * 2.0f * pi
            );
            const float alpha = std::min(p / 0.08f, 1.0f)
                * std::pow(1.0f - p, 0.45f) * flicker;
            const float trail = singMode ? -0.45f * size * p : 0.0f;
            const float lean = std::cos((u2 + p * 1.7f) * 2.0f * pi) * 16.0f
                + (u4 - 0.5f) * 30.0f
                + std::sin((u5 + p * 2.1f) * 2.0f * pi) * 8.0f
                + (singMode ? (u6 - 0.5f) * 40.0f : 0.0f);
            const float sizeFactor = singMode
                ? (0.30f + 0.95f * grow) * (0.55f + 0.85f * u6)
                : (0.35f + 0.85f * grow) * (0.60f + 0.80f * u1);
            out.push_back(FxParticle{
                originX + emitX + (u1 - 0.5f) * boxW * 0.90f
                    + sway + trail,
                originY + (
                    singMode ? (0.00f + 0.66f * u2)
                             : (0.04f + 0.52f * u2)
                ) * boxH - rise,
                lean,
                size * sizeFactor * cool,
                alpha,
                1.0f - 0.35f * p,
                1.0f + 1.05f * p,
            });
        } else if (burst.kind == "fireball") {
            // 火球起爆（镜像 particles fireball 分支）：径向炸开 + 快速
            // 膨胀收缩，配合 spark/shockwave 做点燃/熄灭冲击。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.10f;
            const float p = std::clamp(
                (tau - birth) / std::max(life - birth, 1.0f),
                0.0f, 1.0f
            );
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            const float angle = u1 * 2.0f * pi;
            const float spread = travel * (0.35f + 0.55f * u2) * eased;
            out.push_back(FxParticle{
                originX + std::cos(angle) * spread,
                originY + std::sin(angle) * spread * 0.55f
                    - travel * 0.80f * eased,
                u4 * 360.0f + p * (120.0f + 160.0f * u5),
                size * (0.35f + 0.95f * eased) * (1.0f - 0.15f * p),
                std::min(p / 0.06f, 1.0f) * std::pow(1.0f - p, 1.6f),
            });
        } else if (burst.kind == "flash") {
            // 冲击白闪（镜像 particles flash 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float p = tau / life;
            if (p >= 1.0f) {
                continue;
            }
            out.push_back(FxParticle{
                originX, originY,
                u1 * 90.0f + p * 30.0f,
                size * (0.45f + 0.95f * p),
                std::pow(1.0f - p, 1.5f),
            });
        } else if (burst.kind == "shockwave") {
            // 冲击环（镜像 particles shockwave 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float p = tau / life;
            if (p >= 1.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            out.push_back(FxParticle{
                originX, originY,
                (u1 - 0.5f) * 20.0f,
                size * (0.25f + 1.25f * eased),
                std::pow(1.0f - p, 2.0f) * 0.65f,
            });
        } else if (burst.kind == "spark") {
            // 飞散电火花/火星（镜像 particles spark 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            constexpr float pi = 3.14159265358979323846f;
            const float delay = u3 * 60.0f;
            const float p = std::clamp(
                (tau - delay) / std::max(life - delay, 1.0f),
                0.0f, 1.0f
            );
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float angle = u1 * 2.0f * pi;
            const float speed = travel * (0.60f + 0.90f * u2);
            out.push_back(FxParticle{
                originX + emitX + std::cos(angle) * speed * p,
                originY + std::sin(angle) * speed * p * 0.60f
                    + travel * 0.55f * p * p,
                p * 360.0f * (u4 - 0.5f),
                size * (0.70f + 0.60f * u4) * (1.0f - 0.40f * p),
                std::min(p / 0.05f, 1.0f) * std::pow(1.0f - p, 1.2f),
            });
        } else if (burst.kind == "flame_glow") {
            // 火焰背光（镜像 particles flame_glow 分支）：大尺寸低透明度
            // 火形慢升、缓闪，画在文字之下（front=False）做灼烧辉光。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.25f;
            const float lifeI = std::min(
                life * (0.70f + 0.30f * u4),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * std::pow(p, 1.05f);
            const float sway = std::sin((u2 + p * 1.2f) * 2.0f * pi)
                * size * 0.18f;
            const float glowPulse = 0.72f + 0.28f * std::sin(
                (p * 2.5f + u2) * 2.0f * pi
            );
            out.push_back(FxParticle{
                originX + emitX + (u1 - 0.5f) * boxW * 0.70f + sway,
                originY + (0.05f + 0.25f * u2) * boxH - rise,
                (u4 - 0.5f) * 10.0f,
                size * (0.82f + 0.18f * std::sin(pi * p))
                    * (0.85f + 0.30f * u1),
                std::min(p / 0.15f, 1.0f)
                    * std::pow(1.0f - p, 1.2f) * glowPulse * 0.18f,
                1.0f,
                1.0f + 0.25f * p,
            });
        } else if (burst.kind == "lightning" || burst.kind == "lightning_glow") {
            // 闪电（镜像 particles.burst_particles_at lightning 分支）：
            // 每颗=一次 100~240ms 的短放电，窗口内随机时刻重击；**生长**
            // 用 stretchY 0.18→1.10 并锚住顶端/底端（AE Trim Paths /
            // Direction 关键帧效果）。核心 bolt 在前；同轨迹放大副本
            // （lightning_glow）在后当辉光。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            // 簇状重击节奏（镜像 particles.LIGHTNING_STRIKE_BIRTHS）：
            // 两三连击 + 停顿 + 补击，超出表长按轮次整体后移。
            const auto strikeCount = static_cast<int>(
                sizeof(kLightningStrikeBirths) / sizeof(kLightningStrikeBirths[0])
            );
            const int strikeIndex = i % strikeCount;
            const float strikeBirth = kLightningStrikeBirths[strikeIndex]
                + 0.02f * static_cast<float>(i / strikeCount)
                + (u3 - 0.5f) * kLightningStrikeJitter;
            const float birth = std::clamp(strikeBirth, 0.0f, 0.82f)
                * static_cast<float>(life);
            const float lifeI = std::min(
                100.0f + 140.0f * u4,
                std::max(static_cast<float>(life) - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float growP = std::min(p / 0.45f, 1.0f);
            const float grow = 0.18f + 0.92f * growP;
            const float anchor = burst.kind == "lightning_glow"
                ? size / kLightningGlowSizeScale
                : size;
            const float direction = u5 >= 0.25f ? 1.0f : -1.0f;
            const float strobe = 0.55f + 0.45f * std::abs(
                std::sin((p * 2.6f + u5 * 2.0f) * pi)
            );
            float alpha = std::min(p / 0.06f, 1.0f)
                * std::pow(1.0f - p, 0.65f) * strobe;
            if (burst.kind == "lightning_glow") {
                alpha *= kLightningGlowAlpha;
            }
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 1.10f,
                originY - boxH * 0.08f
                    + (u2 - 0.5f) * boxH * 0.10f
                    + direction * 0.5f * anchor * (grow - 1.0f),
                (u4 - 0.5f) * 20.0f,
                size,
                alpha,
                1.15f,
                grow,
            });
        } else if (burst.kind == "ember") {
            // 余烬火星（镜像 particles ember 分支）：小圆点快速上蹿、
            // 受“重力”减速、横向漂移 + 双频摆动 + 高频闪烁。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.55f;
            const float lifeI = std::min(
                life * (0.50f + 0.50f * u5),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * (1.0f - std::pow(1.0f - p, 2.0f));
            const float drift = (u1 - 0.5f) * travel * 0.55f * p;
            const float sway = std::sin((u2 + p * 2.2f) * 2.0f * pi)
                * size * 1.3f;
            const float twinkle = 0.55f + 0.45f * std::sin(
                (p * 9.0f + u2 * 4.0f) * 2.0f * pi
            );
            out.push_back(FxParticle{
                originX + emitX + (u1 - 0.5f) * boxW * 0.75f
                    + drift + sway,
                originY + (0.10f + 0.30f * u4) * boxH - rise,
                p * 360.0f * (u4 - 0.5f),
                size * (0.55f + 0.55f * u1) * (1.0f - 0.55f * p),
                std::min(p / 0.08f, 1.0f)
                    * std::pow(1.0f - p, 0.9f) * twinkle,
            });
        } else if (burst.kind == "smoke") {
            // 烟（镜像 particles.burst_particles_at smoke 分支，2026-10-22
            // AE 火焰预置 smoke 层）：比火粒晚出生、慢升、持续膨胀、低透明。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.28f;
            const float lifeI = std::min(
                life * (0.72f + 0.28f * u4),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * (1.0f - std::pow(1.0f - p, 1.7f));
            const float sway = std::sin(
                (u2 + p * (0.55f + 0.25f * u5)) * 2.0f * pi
            ) * size * (0.18f + 0.12f * u4);
            const float expand = 0.55f + 0.75f * p;
            out.push_back(FxParticle{
                originX + emitX + (u1 - 0.5f) * boxW * 0.80f + sway,
                originY + (0.02f + 0.30f * u5) * boxH - rise,
                (u2 - 0.5f) * 16.0f + p * 18.0f,
                size * expand * (0.80f + 0.40f * u4),
                std::min(p / 0.22f, 1.0f)
                    * std::pow(1.0f - p, 1.35f) * (0.55f + 0.35f * u5),
            });
        } else if (burst.kind == "ash") {
            // 灰烬（镜像 particles.burst_particles_at ash 分支）：自字框
            // 出生后飘落 + 小幅摇摆 + 边落边缩小熄灭 + 慢自旋。
            constexpr float pi = 3.14159265358979323846f;
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float delay = u3 * life * 0.20f;
            const float p = std::clamp(
                (tau - delay) / std::max(life - delay, 1.0f), 0.0f, 1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            const float fall = (0.35f + 0.45f * u2) * boxH * p;
            const float sway =
                std::sin((u2 + p * 1.2f) * 2.0f * pi) * size * 0.80f;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.8f + sway,
                originY + (u2 - 0.3f) * boxH * 0.4f + fall,
                (u4 - 0.5f) * 120.0f + p * (u3 - 0.5f) * 180.0f,
                size * (0.5f + 0.6f * u1) * (1.0f - 0.45f * p),
                std::min(p * 5.0f, 1.0f) * std::pow(1.0f - p, 1.15f),
            });
        } else if (burst.kind == "petal") {
            // 花瓣飘落（镜像 particles.burst_particles_at petal 分支）：
            // sin 横向摇摆 + 单调竖直行程 + 连续翻转；位置偏移全部取
            // box_w/box_h 比例（星光/音符同约定，不用字号倍数行程——
            // 初版 travel=1.6em 出生过高飘进行间距，2026-10 用户口径）。
            // sweep>0 飘入 ease-out 汇拢、sweep<0 飘散 smoothstep 离场、
            // sweep==0 唱字飘动小行程缓起缓落。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            // AE Box emitter：第一颗立即出生，其余连续错峰（与 Python 镜像）。
            const float spawn = (burst.sweep > 0 && i == 0) ? 0.0f : u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float flutter = std::sin(
                (u2 + p * (0.40f + 0.25f * u5)) * 2.0f * pi
            ) * size * (0.22f + 0.10f * u4);
            const float wind = kPetalWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI = size
                * (kPetalSizeMin + kPetalSizeSpan * depth);
            const float spin = u3 * 360.0f
                + p * (160.0f + 240.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kFallLeanDeg;
            // 不再落到字底：终点在字框下方 1.1~2.3 box_h 的不可观测区，
            // 最后 15% 才淡出；位置随机、不汇聚。
            const float holdFade = p <= 0.85f
                ? 1.0f
                : std::pow(
                    std::max((1.0f - p) / 0.15f, 0.0f), 1.2f
                );
            if (burst.sweep > 0) {
                // 花瓣飘入：非零初速 + 重力加速，穿过文字落到不可观测区。
                const float fall = fallProgress(p);
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float landY = (0.42f + 0.16f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.7f;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                });
            } else if (burst.sweep < 0) {
                // 花瓣飘散：起始高度铺满字框上半部，落到不可观测区；
                // 终点向右、非零初速 + 重力加速。
                const float fall = fallProgress(p);
                const float startX = (u1 - 0.5f) * boxW * 0.85f;
                const float endX = startX + (0.5f + 0.9f * u5) * boxW;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.50f + 0.20f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * (1.0f - p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                });
            } else {
                // 花瓣飘动：字框上半出生，落到不可观测区再淡出。
                const float fall = fallProgress(p);
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.42f + 0.16f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.7f
                        + wind * 0.8f + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * std::sin(pi * p) * speedFade,
                });
            }
        } else if (burst.kind == "feather") {
            // 羽毛（镜像 particles.burst_particles_at feather 分支）：
            // 2026-10-21 走字风——持续向右后方漂 + 小扑动 + 固定后倾，
            // 不再绕中心点大幅钟摆；stretchX 仍做 0.55~1.0 羽片侧翻。
            // sweep>0 飘入落到字底、sweep<0 飘散向右下、sweep==0 唱字缓沉。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            // AE Box emitter：第一颗立即出生，其余连续错峰（与 Python 镜像）。
            const float spawn = (burst.sweep > 0 && i == 0) ? 0.0f : u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            const float wobble = std::sin(
                (u2 + p * (0.32f + 0.28f * u5)) * 2.0f * pi
            ) * size * (0.26f + 0.12f * u4);
            const float wind = kFeatherWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI = size
                * (kFeatherSizeMin + kFeatherSizeSpan * depth);
            const float spin = u3 * 360.0f
                + p * (120.0f + 240.0f * u5)
                    * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kFeatherLeanDeg;
            const float flutter = 0.55f + 0.45f * std::abs(
                std::cos((p * 1.5f + u5) * pi)
            );
            const float holdFade = p <= kFeatherFadeStart
                ? 1.0f
                : std::pow(
                    std::max(
                        (1.0f - p) / (1.0f - kFeatherFadeStart), 0.0f
                    ),
                    1.2f
                );
            if (burst.sweep > 0) {
                // 羽毛飘入：非零初速 + 重力加速，穿过文字落到不可观测区。
                const float fall = fallProgress(p);
                const float landX = (u1 - 0.5f) * boxW * 1.00f;
                const float landY = (0.40f + 0.26f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 1.00f;
                const float startY = -(0.55f + 0.70f * u2) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * std::sin(pi * p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                    flutter,
                    1.0f,
                });
            } else if (burst.sweep < 0) {
                // 羽毛飘散：起始高度铺满字框上半部，落到不可观测区。
                const float fall = fallProgress(p);
                const float startX = (u1 - 0.5f) * boxW * 1.00f;
                const float endX = startX + (0.25f + 0.70f * u5) * boxW;
                const float startY = -(0.55f + 0.70f * u2) * boxH;
                const float landY = (0.44f + 0.30f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * (1.0f - p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                    flutter,
                    1.0f,
                });
            } else {
                // 羽毛飘动：字框上半出生，落到不可观测区再淡出。
                const float fall = fallProgress(p);
                const float startY = -(0.55f + 0.70f * u2) * boxH;
                const float landY = (0.40f + 0.26f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.95f
                        + wind * 0.8f + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.88f + 0.12f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * std::sin(pi * p) * speedFade,
                    flutter,
                    1.0f,
                });
            }
        } else if (burst.kind == "snow" || burst.kind == "snow_solid") {
            // 雪花飘落（镜像 particles.burst_particles_at snow 分支）：
            // 2026-10-21 走字风——持续向右后方漂 + 小扑动 + 固定后倾，
            // 不再绕中心点对称摆；出生锚 = ruby 盒中心-底部二分之一处，
            // 匀速下沉、尺寸随播放由大变小。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            // AE Box emitter：第一颗立即出生，其余连续错峰（与 Python 镜像）。
            const float spawn = (burst.sweep > 0 && i == 0) ? 0.0f : u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float wobble = std::sin(
                (u2 + p * (0.28f + 0.22f * u5)) * 2.0f * pi
            ) * size * (0.26f + 0.12f * u4);
            const float wind = kSnowWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI =
                size * (kSnowSizeMin + kSnowSizeSpan * depth);
            const float spin = u3 * 360.0f
                + p * (80.0f + 140.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kSnowLeanDeg;
            const float birthY = -(0.55f + 0.70f * u2) * boxH;
            const float fall = fallProgress(p);
            const float speedFade = fallSpeedFade(p);
            if (burst.sweep > 0) {
                // 雪花飘入：自字顶上方落入并向字底汇聚；末段随速度渐隐。
                const float landX = (u1 - 0.5f) * boxW * 1.00f;
                const float landY = (0.42f + 0.24f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 1.0f;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 3.0f, 1.0f) * speedFade,
                });
            } else if (burst.sweep < 0) {
                // 雪花飘散：从主文字上方出发，向字底汇聚；终点向右，
                // 末段随速度渐隐。
                const float landX = (u1 - 0.5f) * boxW * 1.00f;
                const float endX = landX + (0.35f + 0.6f * u5) * boxW;
                const float landY = (0.44f + 0.30f * u2) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 1.0f;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 3.0f, 1.0f) * speedFade,
                });
            } else {
                // 雪花飘动（唱字）：与出入场同一起始高度，向字底汇聚；
                // 末段随速度渐隐。
                const float landY = (0.42f + 0.24f * u4) * boxH;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 1.00f
                        + wind * 0.8f + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 4.0f, 1.0f) * std::sin(pi * p) * speedFade,
                });
            }
        }
    }
    return out;
}

namespace {

struct ColumnUnitRgb {
    float red = 1.0f;
    float green = 1.0f;
    float blue = 1.0f;
};

ColumnUnitRgb columnUnitRgb(const RgbaColor &color) {
    return ColumnUnitRgb{
        static_cast<float>(color.red) / 255.0f,
        static_cast<float>(color.green) / 255.0f,
        static_cast<float>(color.blue) / 255.0f,
    };
}

inline float smoothStep01(float value) {
    const float v = std::clamp(value, 0.0f, 1.0f);
    return v * v * (3.0f - 2.0f * v);
}

void writeColumnPixel(
    std::vector<std::uint8_t> &pixels,
    std::size_t offset,
    float red,
    float green,
    float blue,
    float alpha
) {
    const float safeAlpha = std::clamp(alpha, 0.0f, 1.0f);
    const auto channel = [](float value) {
        return static_cast<std::uint8_t>(std::lround(
            std::clamp(value, 0.0f, 1.0f) * 255.0f
        ));
    };
    // BGRA 预乘（DXGI_FORMAT_B8G8R8A8_UNORM + PREMULTIPLIED）。
    pixels[offset + 0] = channel(blue);
    pixels[offset + 1] = channel(green);
    pixels[offset + 2] = channel(red);
    pixels[offset + 3] = channel(safeAlpha);
}

struct ColumnBubble {
    float centerX = 0.0f;
    float centerY = 0.0f;
    float radiusPx = 1.0f;
    float fade = 0.0f;
};

}  // namespace

std::vector<std::uint8_t> scanlineColumnPixels(
    const std::string &shape,
    const RgbaColor &outer,
    const RgbaColor &inner,
    const RgbaColor &core,
    const PaintStyle *fill,
    int timeMs,
    unsigned width,
    unsigned height,
    float gradientLeft,
    float gradientWidth
) {
    const unsigned rasterWidth = std::max(width, 1u);
    const unsigned rasterHeight = std::max(height, 1u);
    std::vector<std::uint8_t> pixels(
        static_cast<std::size_t>(rasterWidth) * rasterHeight * 4, 0
    );
    // 水泡改为 backends 里逐帧椭圆粒子；这里只负责「烈焰棒」。
    if (shape != "flame") {
        return pixels;
    }
    const ColumnUnitRgb outerRgb = columnUnitRgb(outer);
    const ColumnUnitRgb innerRgb = columnUnitRgb(inner);
    const ColumnUnitRgb coreRgb = columnUnitRgb(core);
    const float t = static_cast<float>(timeMs) / 1000.0f;
    const float widthF = static_cast<float>(rasterWidth);
    const float heightF = static_cast<float>(rasterHeight);
    constexpr float kTwoPi = 6.28318530717958647692f;
    bool hasGradient = false;
    bool gradientHorizontal = false;
    std::vector<std::pair<float, ColumnUnitRgb>> gradientStops;
    if (fill != nullptr
        && (fill->mode == "gradient_horizontal"
            || fill->mode == "gradient_vertical"
            || fill->mode == "split_vertical")
        && fill->stops.size() >= 2) {
        gradientHorizontal = fill->mode == "gradient_horizontal";
        gradientStops.reserve(fill->stops.size());
        for (const PaintStop &stop : fill->stops) {
            gradientStops.emplace_back(
                std::clamp(stop.position, 0.0f, 1.0f),
                columnUnitRgb(stop.color)
            );
        }
        std::sort(
            gradientStops.begin(), gradientStops.end(),
            [](const auto &left, const auto &right) {
                return left.first < right.first;
            }
        );
        hasGradient = gradientStops.size() >= 2;
    }
    const auto gradientColorAt = [&](float coord) {
        coord = std::clamp(coord, 0.0f, 1.0f);
        if (fill->mode == "split_vertical") {
            std::size_t index = 0;
            for (std::size_t i = 0; i < gradientStops.size(); ++i) {
                if (gradientStops[i].first <= coord) {
                    index = i;
                }
            }
            return gradientStops[index].second;
        }
        if (coord <= gradientStops.front().first) {
            return gradientStops.front().second;
        }
        if (coord >= gradientStops.back().first) {
            return gradientStops.back().second;
        }
        for (std::size_t i = 1; i < gradientStops.size(); ++i) {
            if (coord > gradientStops[i].first) {
                continue;
            }
            const auto &left = gradientStops[i - 1];
            const auto &right = gradientStops[i];
            const float span = std::max(right.first - left.first, 1.0e-6f);
            const float mix = (coord - left.first) / span;
            return ColumnUnitRgb{
                left.second.red * (1.0f - mix) + right.second.red * mix,
                left.second.green * (1.0f - mix) + right.second.green * mix,
                left.second.blue * (1.0f - mix) + right.second.blue * mix
            };
        }
        return gradientStops.back().second;
    };
    const auto lightenUnit = [](const ColumnUnitRgb &color, float amount) {
        return ColumnUnitRgb{
            color.red + (1.0f - color.red) * amount,
            color.green + (1.0f - color.green) * amount,
            color.blue + (1.0f - color.blue) * amount
        };
    };

    struct RibbonSpec {
        int edgeLayer;
        int centerLayer;
        float turns;
        float phase;
        float amplitude;
        float half;
        float strength;
    };
    const RibbonSpec specs[4] = {
        {0, 1, 1.85f, 0.00f, 0.46f, 0.180f, 0.92f},
        {0, 1, 2.12f, 0.52f, 0.42f, 0.145f, 0.82f},
        {1, 2, 1.72f, 0.24f, 0.39f, 0.105f, 0.92f},
        {2, 2, 1.55f, 0.55f, 0.32f, 0.048f, 0.70f},
    };
    struct RibbonRow {
        float center = 0.0f;
        float half = 0.01f;
        float front = 0.0f;
        float back = 0.0f;
        float flicker = 1.0f;
        float burn = 1.0f;
    };

    for (unsigned y = 0; y < rasterHeight; ++y) {
        const float v = (static_cast<float>(y) + 0.5f) / heightF;
        const float vertical = smoothStep01((v - 0.03f) / 0.07f)
            * (1.0f - smoothStep01((v - 0.94f) / 0.05f));
        const float breath = 0.5f + 0.5f * std::sin(t * 2.8f);
        const float ribbonScale = 0.96f + 0.08f * breath;
        RibbonRow rows[4];
        for (int i = 0; i < 4; ++i) {
            const RibbonSpec &spec = specs[i];
            const float angle = v * spec.turns * kTwoPi
                + spec.phase * kTwoPi
                + t * (2.0f + 0.60f * spec.turns);
            const float depth = std::cos(angle);
            const float turb = std::sin(v * 13.0f + t * 5.0f + spec.phase * 6.0f)
                * std::sin(v * 7.0f - t * 2.5f + spec.phase * 3.0f);
            rows[i].center = spec.amplitude * ribbonScale * std::sin(angle)
                + 0.02f * turb;
            rows[i].front = std::clamp(depth * 2.6f, 0.0f, 1.0f);
            rows[i].back = std::clamp(-depth * 2.6f, 0.0f, 1.0f);
            rows[i].flicker = 0.88f
                + 0.12f * std::sin(angle * 2.0f - t * 3.1f + spec.phase * 5.0f);
            rows[i].burn = 0.85f
                + 0.15f * std::sin(v * 55.0f - t * 12.0f + spec.phase * 8.0f);
            const float swell = std::sin(
                angle * 2.0f + t * 2.3f + spec.phase * 7.0f
            );
            rows[i].half = std::max(
                spec.half * ribbonScale
                    * (0.45f + 0.55f * swell * swell)
                    * (0.85f + 0.25f * std::abs(turb)),
                0.008f
            );
        }

        const float rodHalf = 0.130f + 0.018f * std::sin(v * 18.0f + t * 6.0f);

        for (unsigned x = 0; x < rasterWidth; ++x) {
            const float u = (static_cast<float>(x) + 0.5f) / widthF
                * 2.0f - 1.0f;
            ColumnUnitRgb outerColor = outerRgb;
            ColumnUnitRgb innerColor = innerRgb;
            ColumnUnitRgb coreColor = coreRgb;
            if (hasGradient) {
                const float coord = gradientHorizontal
                    ? gradientLeft
                        + (u + 1.0f) * 0.5f * gradientWidth
                    : v;
                outerColor = gradientColorAt(coord);
                innerColor = lightenUnit(outerColor, 0.20f);
                coreColor = lightenUnit(outerColor, 0.50f);
            }
            const auto layerColor = [&](int layer) -> const ColumnUnitRgb & {
                return layer == 0 ? outerColor
                    : (layer == 1 ? innerColor : coreColor);
            };
            float rgb[3] = {0.0f, 0.0f, 0.0f};
            float alpha = 0.0f;
            const auto apply = [&](const ColumnUnitRgb &color, float a) {
                const float layer = std::clamp(a, 0.0f, 1.0f);
                rgb[0] = color.red * layer + rgb[0] * (1.0f - layer);
                rgb[1] = color.green * layer + rgb[1] * (1.0f - layer);
                rgb[2] = color.blue * layer + rgb[2] * (1.0f - layer);
                alpha = layer + alpha * (1.0f - layer);
            };

            // 后半圈缎带：先画，随后被柱身遮住。
            for (int i = 0; i < 4; ++i) {
                if (rows[i].back <= 0.0f) {
                    continue;
                }
                const float dx = (u - rows[i].center) / rows[i].half;
                const float band = std::exp(-std::pow(dx, 4.0f));
                const float glow = std::exp(
                    -std::pow(std::abs(dx) / 1.5f, 2.0f)
                ) * 0.12f;
                const float a = std::clamp(
                    (band + glow) * rows[i].back * rows[i].flicker
                        * rows[i].burn * vertical * specs[i].strength * 0.38f,
                    0.0f,
                    1.0f
                );
                const float mix = std::pow(
                    std::clamp(1.0f - std::abs(dx), 0.0f, 1.0f), 1.4f
                );
                const ColumnUnitRgb &edge = layerColor(specs[i].edgeLayer);
                const ColumnUnitRgb &center = layerColor(specs[i].centerLayer);
                const ColumnUnitRgb color{
                    (edge.red * (1.0f - mix)
                        + center.red * mix) * 0.40f
                        + innerColor.red * 0.60f,
                    (edge.green * (1.0f - mix)
                        + center.green * mix) * 0.40f
                        + innerColor.green * 0.60f,
                    (edge.blue * (1.0f - mix)
                        + center.blue * mix) * 0.40f
                        + innerColor.blue * 0.60f
                };
                apply(color, a);
            }

            // 中心高亮棒由 Direct2D 的普通扫字线按字形绘制；
            // 这里补一层无硬边的软实体光晕，给外围火圈体积感。
            const float haloHalf = 0.210f + 0.030f * std::sin(t * 3.1f);
            const float haloCore = std::exp(
                -std::pow(std::abs(u) / haloHalf, 2.2f)
            ) * vertical;
            const float haloWide = std::exp(
                -std::pow(std::abs(u) / (haloHalf * 2.6f), 2.0f)
            ) * vertical;
            const float haloAlpha = std::clamp(
                0.32f * haloCore + 0.14f * haloWide, 0.0f, 0.70f
            );
            const ColumnUnitRgb haloColor{
                innerColor.red * 0.85f + coreColor.red * 0.15f,
                innerColor.green * 0.85f + coreColor.green * 0.15f,
                innerColor.blue * 0.85f + coreColor.blue * 0.15f
            };
            apply(haloColor, haloAlpha);

            // 前半圈缎带：压在柱身上。
            for (int i = 0; i < 4; ++i) {
                if (rows[i].front <= 0.0f) {
                    continue;
                }
                const float dx = (u - rows[i].center) / rows[i].half;
                const float band = std::exp(-std::pow(dx, 4.0f));
                const float glow = std::exp(
                    -std::pow(std::abs(dx) / 1.5f, 2.0f)
                ) * 0.12f;
                const float a = std::clamp(
                    (band + glow) * rows[i].front * rows[i].flicker
                        * rows[i].burn * vertical * specs[i].strength,
                    0.0f,
                    1.0f
                );
                const float mix = std::pow(
                    std::clamp(1.0f - std::abs(dx), 0.0f, 1.0f), 1.4f
                );
                const ColumnUnitRgb &edge = layerColor(specs[i].edgeLayer);
                const ColumnUnitRgb &center = layerColor(specs[i].centerLayer);
                const ColumnUnitRgb color{
                    edge.red * (1.0f - mix) + center.red * mix,
                    edge.green * (1.0f - mix) + center.green * mix,
                    edge.blue * (1.0f - mix) + center.blue * mix
                };
                apply(color, a);
            }

            writeColumnPixel(
                pixels,
                (static_cast<std::size_t>(y) * rasterWidth + x) * 4,
                rgb[0],
                rgb[1],
                rgb[2],
                alpha
            );
        }
    }
    return pixels;
}

// ---------------------------------------------------------------------------
// 自定义图片扫字线（scanline_shape == "image"）的取色层级装饰。
// 与 Python scanline_image._decorated_scanline_frame / band._edge_source
// 同算法口径：膨胀 = 可分离 max 滤波、发光 = 3 趟盒式模糊（等效高斯）、
// 上色 = 预乘 alpha 剪影、合成 = 预乘 source-over。
namespace {

void dilateAlphaInPlace(
    std::vector<float> &alpha, int width, int height, int radius
) {
    if (radius <= 0) {
        return;
    }
    std::vector<float> temp(alpha.size());
    for (int y = 0; y < height; ++y) {
        const float *row = alpha.data() + static_cast<std::size_t>(y) * width;
        float *out = temp.data() + static_cast<std::size_t>(y) * width;
        for (int x = 0; x < width; ++x) {
            float best = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                const int xx = std::clamp(x + k, 0, width - 1);
                best = std::max(best, row[xx]);
            }
            out[x] = best;
        }
    }
    for (int x = 0; x < width; ++x) {
        for (int y = 0; y < height; ++y) {
            float best = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                const int yy = std::clamp(y + k, 0, height - 1);
                best = std::max(
                    best,
                    temp[static_cast<std::size_t>(yy) * width + x]
                );
            }
            alpha[static_cast<std::size_t>(y) * width + x] = best;
        }
    }
}

void boxBlurAlphaInPlace(
    std::vector<float> &alpha, int width, int height, int radius
) {
    if (radius <= 0) {
        return;
    }
    const auto window = static_cast<float>(radius * 2 + 1);
    std::vector<float> temp(alpha.size());
    for (int pass = 0; pass < 3; ++pass) {
        for (int y = 0; y < height; ++y) {
            const float *row = alpha.data() + static_cast<std::size_t>(y) * width;
            float *out = temp.data() + static_cast<std::size_t>(y) * width;
            float sum = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                sum += row[std::clamp(k, 0, width - 1)];
            }
            for (int x = 0; x < width; ++x) {
                out[x] = sum / window;
                const int add = std::clamp(x + radius + 1, 0, width - 1);
                const int sub = std::clamp(x - radius, 0, width - 1);
                sum += row[add] - row[sub];
            }
        }
        for (int x = 0; x < width; ++x) {
            float sum = 0.0f;
            for (int k = -radius; k <= radius; ++k) {
                sum += temp[static_cast<std::size_t>(std::clamp(k, 0, height - 1))
                             * width + x];
            }
            for (int y = 0; y < height; ++y) {
                alpha[static_cast<std::size_t>(y) * width + x]
                    = sum / window;
                const int add = std::clamp(y + radius + 1, 0, height - 1);
                const int sub = std::clamp(y - radius, 0, height - 1);
                sum += temp[static_cast<std::size_t>(add) * width + x]
                    - temp[static_cast<std::size_t>(sub) * width + x];
            }
        }
    }
}

}  // namespace

ScanlineImageComposite scanlineImageCompositePixels(
    const std::uint8_t *frame,
    int bytesPerLine,
    int width,
    int height,
    const ScanlineImageDecor &decor
) {
    ScanlineImageComposite out;
    if (frame == nullptr || width <= 0 || height <= 0 || bytesPerLine <= 0) {
        return out;
    }
    const int sourceStride = bytesPerLine;

    int margin = 0;
    if (decor.glow && decor.glowRadius > 0) {
        margin = std::max(margin, decor.glowRadius * 3);
    }
    if (decor.shadow) {
        margin = std::max(
            {margin,
             static_cast<int>(std::ceil(std::abs(decor.shadowOffsetX) + 1.0f)),
             static_cast<int>(std::ceil(std::abs(decor.shadowOffsetY) + 1.0f))}
        );
    }
    if (decor.stroke2Width > 0) {
        margin = std::max(margin, decor.stroke2Width + 1);
    }
    if (decor.strokeWidth > 0) {
        margin = std::max(margin, decor.strokeWidth + 1);
    }

    const int canvasWidth = width + margin * 2;
    const int canvasHeight = height + margin * 2;
    std::vector<float> base(static_cast<std::size_t>(canvasWidth) * canvasHeight, 0.0f);
    for (int y = 0; y < height; ++y) {
        const std::uint8_t *row = frame
            + static_cast<std::size_t>(y) * sourceStride;
        float *outRow = base.data()
            + static_cast<std::size_t>(y + margin) * canvasWidth + margin;
        for (int x = 0; x < width; ++x) {
            outRow[x] = static_cast<float>(row[x * 4 + 3]);
        }
    }
    out.pixels.assign(
        static_cast<std::size_t>(canvasWidth) * canvasHeight * 4, 0
    );
    out.width = canvasWidth;
    out.height = canvasHeight;
    out.margin = margin;

    // 预乘 source-over：把一层「alpha × 纯色」的剪影按 (shiftX, shiftY)
    // 平移后叠进画布（图层顺序与 Python 一致：发光 → 阴影 → 描边2 →
    // 描边 → 本体）。
    const auto overlayLayer = [&](
        const std::vector<float> &layer,
        const RgbaColor &color,
        int shiftX,
        int shiftY
    ) {
        const float colorAlpha = static_cast<float>(color.alpha) / 255.0f;
        for (int y = 0; y < canvasHeight; ++y) {
            const int ty = y + shiftY;
            if (ty < 0 || ty >= canvasHeight) {
                continue;
            }
            for (int x = 0; x < canvasWidth; ++x) {
                const int tx = x + shiftX;
                if (tx < 0 || tx >= canvasWidth) {
                    continue;
                }
                const float value = std::clamp(
                    layer[static_cast<std::size_t>(y) * canvasWidth + x],
                    0.0f,
                    255.0f
                ) / 255.0f * colorAlpha;
                if (value <= 0.0f) {
                    continue;
                }
                std::uint8_t *dst = out.pixels.data()
                    + (static_cast<std::size_t>(ty) * canvasWidth + tx) * 4;
                const float keep = 1.0f - value;
                dst[0] = static_cast<std::uint8_t>(std::lround(
                    color.blue * value + dst[0] * keep
                ));
                dst[1] = static_cast<std::uint8_t>(std::lround(
                    color.green * value + dst[1] * keep
                ));
                dst[2] = static_cast<std::uint8_t>(std::lround(
                    color.red * value + dst[2] * keep
                ));
                dst[3] = static_cast<std::uint8_t>(std::lround(
                    255.0f * value + dst[3] * keep
                ));
            }
        }
    };

    if (decor.glow && decor.glowRadius > 0) {
        std::vector<float> glow = base;
        boxBlurAlphaInPlace(glow, canvasWidth, canvasHeight, decor.glowRadius);
        overlayLayer(glow, decor.glowColor, 0, 0);
    }
    if (decor.shadow) {
        overlayLayer(
            base,
            decor.shadowColor,
            static_cast<int>(std::lround(decor.shadowOffsetX)),
            static_cast<int>(std::lround(decor.shadowOffsetY))
        );
    }
    if (decor.stroke2Width > 0) {
        std::vector<float> stroke2 = base;
        dilateAlphaInPlace(
            stroke2, canvasWidth, canvasHeight, decor.stroke2Width
        );
        overlayLayer(stroke2, decor.stroke2Color, 0, 0);
    }
    if (decor.strokeWidth > 0) {
        std::vector<float> stroke = base;
        dilateAlphaInPlace(
            stroke, canvasWidth, canvasHeight, decor.strokeWidth
        );
        overlayLayer(stroke, decor.strokeColor, 0, 0);
    }
    // 本体：实色上色 = 按强度混合（100 = 纯目标色剪影；部分强度保留
    // 原色，alpha 始终用素材原值——与 Python _tinted 同公式）。混合在
    // 预乘空间进行：out = src*(1-k) + tintRGB*k*sa。
    if (decor.tint) {
        if (decor.tintStrength >= 100) {
            overlayLayer(base, decor.tintColor, 0, 0);
            return out;
        }
        const float k = static_cast<float>(
            std::clamp(decor.tintStrength, 0, 100)
        ) / 100.0f;
        const float keep = 1.0f - k;
        for (int y = 0; y < height; ++y) {
            const std::uint8_t *row = frame
                + static_cast<std::size_t>(y) * sourceStride;
            std::uint8_t *dstRow = out.pixels.data()
                + (static_cast<std::size_t>(y + margin) * canvasWidth
                   + margin) * 4;
            for (int x = 0; x < width; ++x) {
                const std::uint8_t sa = row[x * 4 + 3];
                if (sa == 0) {
                    continue;
                }
                for (int channel = 0; channel < 3; ++channel) {
                    const float source = row[x * 4 + channel];
                    const float tintChannel = channel == 0
                        ? decor.tintColor.blue
                        : (channel == 1
                               ? decor.tintColor.green
                               : decor.tintColor.red);
                    dstRow[x * 4 + channel] = static_cast<std::uint8_t>(
                        std::lround(
                            source * keep + tintChannel * k
                        )
                    );
                }
                dstRow[x * 4 + 3] = sa;
            }
        }
        return out;
    }
    for (int y = 0; y < height; ++y) {
        const std::uint8_t *row = frame
            + static_cast<std::size_t>(y) * sourceStride;
        std::uint8_t *dstRow = out.pixels.data()
            + (static_cast<std::size_t>(y + margin) * canvasWidth + margin) * 4;
        for (int x = 0; x < width; ++x) {
            const std::uint8_t sa = row[x * 4 + 3];
            if (sa == 0) {
                continue;
            }
            const float keep = 1.0f
                - static_cast<float>(sa) / 255.0f;
            for (int channel = 0; channel < 4; ++channel) {
                dstRow[x * 4 + channel] = static_cast<std::uint8_t>(
                    std::lround(
                        row[x * 4 + channel]
                        + dstRow[x * 4 + channel] * keep
                    )
                );
            }
        }
    }
    return out;
}

}  // namespace krok::subtitle::native::direct2d

