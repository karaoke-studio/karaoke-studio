#include "particle_frame.h"
#include "../unit_hash.h"
namespace krok::subtitle::native::effects::particles {
std::vector<FxParticle> scanline(const Frame& frame) {
 const auto& [burst, tau, life, size, travel, emitX, originX, originY, boxW, boxH] = frame;
 const auto seed = burst.seed;
 std::vector<FxParticle> out;

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
}
