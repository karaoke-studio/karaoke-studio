#pragma once
#include <string>

namespace krok::subtitle::native::effects {
struct GeoCharState {
    float opacity = 1.0f;
    float dx = 0.0f;
    float dy = 0.0f;
    float rotation = 0.0f;
    float scaleX = 1.0f;
    float scaleY = 1.0f;
    // glow_in / glow_out 的辉光状态（多级描边光晕；见 kGlowHaloStrokes）。
    float glowAlpha = 0.0f;
    float glowRadiusEm = 0.0f;
};

/// 逐字几何特效（tracking_in / wave_in / scatter_out / converge_out）。
/// ``fontPx`` 为该行样式字号（D2D 行空间，已含 layoutReferenceScale）；
/// ``startMs`` 为编排窗口起点（入场=显示起点，退场=退场起点）。
/// 编排与 char_fade 同构（350ms 错峰 + 250ms 行程），「入场/退场动画
/// 时长」仅作 >0 的播放门，值不进本函数。
/// ``hasCenters`` 为真时 converge_out 用真实字符/行中心，否则 index 估算。
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
);


}
