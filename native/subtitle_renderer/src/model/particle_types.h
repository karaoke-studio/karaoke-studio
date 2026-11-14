#pragma once

#include "paint_types.h"

namespace krok::subtitle::native {

struct ParticleBurst {
    std::string kind;  // sparkle / ripple / twinkle / twinkle_classic / note / assemble / dissolve
    // 可选 sprite 覆盖（火形/雷形预设）：空 = 按 kind 取默认 sprite。
    std::string sprite;
    std::string anchor;  // "line" | "char"
    int charIndex = -1;
    int startMs = 0;
    int endMs = 0;
    int count = 0;
    std::uint32_t seed = 0;
    float sizePx = 28.0f;
    float travelPx = 120.0f;
    // 唱字火焰按簇横向扫过的发射点偏移（归一化 × 字框宽；0 = 不偏移）。
    float emitX = 0.0f;
    // 扫字线特效（kind="scanline"）的锋面发射窗口（ms）：出生时刻换算成
    // 锋面横向进度；窗口尾部剩余部分是粒子寿命。0 = 整个窗口都是发射窗
    //（旧 IR / 其余 kind 缺省，行为不变）。
    int emitMs = 0;
    // 粒子速度调整（2026-10，镜像 particles.burst_particles_at）：burst 窗口
    // 已按速度缩放，求值器把 tau/life 除以 timeScale 映射回标准速率，使内部
    // 绝对发射间隔（涟漪环 150ms / 拼接 90ms 等）与寿命同比缩放。旧 IR /
    // 原速 = 1，逐位等价于改造前。
    float timeScale = 1.0f;
    bool front = true;
    bool reverse = false;
    // sparkle 整句扫过方向：+1 入场（左→右）、-1 退场（右→左）、0 不扫。
    int sweep = 0;
    RgbaColor color{255, 255, 255, 255};
    // 2026-10 颜色模式装饰规格（跟随字体/复用配色方案）：hasPaint=false
    // 时按 ``color`` 实心绘制（旧 IR / 单独颜色档）。宽度为物理 px（投影
    // 时随 scale 同比缩放，与 sizePx 同基准）；涟漪的非横向渐变由渲染端
    // 做径向映射（每环按扩散进度在渐变轴上采样实心色：内圈新环=起点
    // 色、外圈老环=终点色）。
    bool hasPaint = false;
    PaintStyle fill;
    PaintStyle stroke;
    PaintStyle stroke2;
    float strokeWidth = 0.0f;
    float stroke2Width = 0.0f;
    // 取色层级「+装饰/全有」的装饰层（2026-10）：kind 由来源角色方案的
    // decoration_kind 选定——"shadow" 用 decor 色画行空间常量偏移的整剪影
    // （偏移 decorOffsetX/Y，px，与 strokeWidth 同基准）；"glow" 用多级
    // 描边近似弥散晕（半径 decorRadius px + decorConcentration 档，与
    // kGlowHaloStrokes 配方同源）。fill 为装饰色（配色态 shadow 槽）。
    bool hasDecor = false;
    std::string decorKind;
    PaintStyle decor;
    float decorOffsetX = 0.0f;
    float decorOffsetY = 0.0f;
    float decorRadius = 0.0f;
    int decorConcentration = 0;
    // 行锚点星光的逐字实色表（#RRGGBB；空 = 不启用）。
    std::vector<RgbaColor> charColors;
    bool operator==(const ParticleBurst &) const = default;
};

}  // namespace krok::subtitle::native
