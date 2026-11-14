#pragma once

namespace krok::subtitle::native::effects {
struct GlowHaloStroke {
    float widthEm;
    float strength;
};
inline constexpr GlowHaloStroke kGlowHaloStrokes[] = {
    {0.10f, 0.42f},
    {0.22f, 0.24f},
    {0.38f, 0.14f},
    {0.58f, 0.08f},
    {0.82f, 0.045f},
};
// 辉光浮现/消散 = 细密横向回声（镜像 transitions.GLOW_ECHO_*）：
// 每侧 N 个字形重影副本、固定间距、亮度几何衰减。
inline constexpr int kGlowEchoCopies = 14;
inline constexpr float kGlowEchoPitchEm = 0.12f;
inline constexpr float kGlowEchoDecay = 0.82f;

/// 唱字描边闪光常量（镜像 painter.STROKE_FLASH_*）。
inline constexpr int kStrokeFlashMs = 240;
inline constexpr float kStrokeFlashAlpha = 0.85f;
inline constexpr float kStrokeFlashWidthBoost = 0.6f;
inline constexpr float kStrokeFlashMinWidthEm = 0.03f;


}
