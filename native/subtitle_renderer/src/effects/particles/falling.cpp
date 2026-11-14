#include "particle_frame.h"
#include "../unit_hash.h"
namespace krok::subtitle::native::effects::particles {
std::vector<FxParticle> falling(const Frame& frame) {
 const auto& [burst, tau, life, size, travel, emitX, originX, originY, boxW, boxH] = frame;
 const auto seed = burst.seed;
 std::vector<FxParticle> out;
 int starPrevQ = -1;
 out.reserve(static_cast<std::size_t>(std::max(burst.count, 0)));
 for (int i = 0; i < burst.count; ++i) {
 const auto index = seed + static_cast<std::uint32_t>(i);
        if (burst.kind == "petal") {
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
}
