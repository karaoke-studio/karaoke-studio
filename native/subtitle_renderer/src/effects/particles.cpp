#include "particles.h"
#include "particles/particle_frame.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
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

    const particles::Frame frame{burst, tau, life, size, travel, emitX, originX, originY, boxW, boxH};
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

    if (burst.kind == "scanline") return particles::scanline(frame);
    if (burst.kind == "petal" || burst.kind == "feather" || burst.kind == "snow" || burst.kind == "snow_solid") return particles::falling(frame);
    if (burst.kind == "sparkle" || burst.kind == "twinkle" || burst.kind == "twinkle_classic" || burst.kind == "assemble" || burst.kind == "dissolve" || burst.kind == "note" || burst.kind == "heart") return particles::decoration(frame);
    return particles::energy(frame);
}

}  // namespace krok::subtitle::native::effects
