#include "particle_frame.h"
#include "../unit_hash.h"
namespace krok::subtitle::native::effects::particles {
std::vector<FxParticle> decoration(const Frame& frame) {
 const auto& [burst, tau, life, size, travel, emitX, originX, originY, boxW, boxH] = frame;
 const auto seed = burst.seed;
 std::vector<FxParticle> out;
 int starPrevQ = -1;
 out.reserve(static_cast<std::size_t>(std::max(burst.count, 0)));
 for (int i = 0; i < burst.count; ++i) {
 const auto index = seed + static_cast<std::uint32_t>(i);
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
        }

 }
 return out;
}
}
