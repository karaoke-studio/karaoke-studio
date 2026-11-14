#include "particle_frame.h"
#include "../unit_hash.h"
namespace krok::subtitle::native::effects::particles {
std::vector<FxParticle> energy(const Frame& frame) {
 const auto& [burst, tau, life, size, travel, emitX, originX, originY, boxW, boxH] = frame;
 const auto seed = burst.seed;
 std::vector<FxParticle> out;
 int starPrevQ = -1;
 out.reserve(static_cast<std::size_t>(std::max(burst.count, 0)));
 for (int i = 0; i < burst.count; ++i) {
 const auto index = seed + static_cast<std::uint32_t>(i);
        if (burst.kind == "flame" || burst.kind == "flame_alt") {
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
        }

 }
 return out;
}
}
