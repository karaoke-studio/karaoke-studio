#pragma once
#include "../model/particle_types.h"

namespace krok::subtitle::native::effects {
struct FxParticle {
    float x = 0.0f;
    float y = 0.0f;
    float rotationDeg = 0.0f;
    float sizePx = 0.0f;
    float alpha = 0.0f;
    // 火焰火舌的速度拉伸（sprite 双轴缩放倍率；其余 kind 恒 1）：
    // 越升越窄越长，镜像 Python ParticleDraw.stretch_x/stretch_y。
    float stretchX = 1.0f;
    float stretchY = 1.0f;
};

/// 一个 ParticleBurst 在 tMs 的全部粒子（镜像 particles.burst_particles_at）。
std::vector<FxParticle> burstParticlesAt(
    const ParticleBurst &burst,
    int tMs,
    float originX,
    float originY,
    float boxW,
    float boxH
);

inline const char *fxSpriteForKind(const std::string &kind) {
    if (kind == "ripple") {
        return "ring";
    }
    if (kind == "note") {
        return "note";
    }
    if (kind == "heart") {
        return "heart";
    }
    if (kind == "flame") {
        return "flame";
    }
    if (kind == "flame_tall") {
        return "flame_tall";
    }
    if (kind == "flame_wide") {
        return "flame_wide";
    }
    if (kind == "flame_curl") {
        return "flame_curl";
    }
    if (kind == "flame_alt") {
        return "flame_alt";
    }
    if (kind == "flame_alt_b") {
        return "flame_alt_b";
    }
    if (kind == "fireball") {
        return "flame";
    }
    if (kind == "flash") {
        return "star4";
    }
    if (kind == "shockwave") {
        return "ring";
    }
    if (kind == "spark") {
        return "dot";
    }
    if (kind == "flame_glow") {
        return "flame";
    }
    if (kind == "ember") {
        return "dot";
    }
    if (kind == "ash") {
        return "ash";
    }
    if (kind == "smoke") {
        return "smoke";
    }
    if (kind == "assemble" || kind == "dissolve") {
        return "pixel";
    }
    if (kind == "petal") {
        return "petal";
    }
    if (kind == "feather") {
        return "feather";
    }
    if (kind == "lightning" || kind == "lightning_glow") {
        return "bolt";
    }
    if (kind == "snow") {
        return "snow";
    }
    if (kind == "snow_solid") {
        return "snow_solid";
    }
    return "star4";
}


int fallEntryLeadMs(const std::string& effect);

}
