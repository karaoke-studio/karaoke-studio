#pragma once

#include "paint_types.h"

namespace krok::subtitle::native {

struct LineBandStyle {
    bool enabled = false;
    std::wstring imagePath;
    std::string fillMode = "three_slice";
    std::string midMode = "repeat";
    float capLeft = 0.0f;
    float capRight = 0.0f;
    // 源图像素为单位的端点宽（裁中段位图用；capLeft/capRight 是场景像素）。
    int capLeftSrc = 0;
    int capRightSrc = 0;
    // 中段预拼条的源图裁切边界（预设设计端点），与显示端点解耦。
    // 图层效果（PS 图层样式口径）：阴影/描边/发光各自独立开关与配置。
    struct Effect {
        bool enabled = false;
        bool hasColor = false;
        RgbaColor color{};
        float size = 0.0f;
        float offsetX = 0.0f;
        float offsetY = 0.0f;
        // PS/AE 标准参数：扩展（像素）、范围（0..1）、描边位置。
        float spreadPx = 0.0f;
        float range = 0.5f;
        std::string position = "outside";

        bool operator==(const Effect &other) const {
            return enabled == other.enabled && hasColor == other.hasColor
                && color == other.color && size == other.size
                && offsetX == other.offsetX && offsetY == other.offsetY
                && spreadPx == other.spreadPx && range == other.range
                && position == other.position;
        }
    };
    Effect shadow;
    Effect stroke;
    Effect glow;    int srcCapLeft = 0;
    int srcCapRight = 0;
    float padTop = 20.0f;
    float padBottom = 20.0f;
    float padLeft = 40.0f;
    float padRight = 40.0f;
    bool includeRuby = false;
    float offsetY = 0.0f;
    float opacity = 1.0f;
    bool hasTint = false;
    RgbaColor tint{255, 255, 255, 255};
    int tintStrengthPct = 100;
    // 独立谱线层（五线谱）：白色素材 + 用户谱线颜色。
    std::wstring linesPath;
    bool hasLineColor = false;
    RgbaColor lineColor{0, 0, 0, 255};
    // 独立谱号层（含升降号）：白色素材 + 用户谱号颜色。
    std::wstring clefPath;
    bool hasClefColor = false;
    RgbaColor clefColor{255, 255, 255, 255};
    // 非透明部分的投影 / 发光。
    std::string edgeKind = "none";
    bool hasEdgeColor = false;
    RgbaColor edgeColor{0, 0, 0, 255};
    float edgeSize = 12.0f;
    float edgeOffsetX = 8.0f;
    float edgeOffsetY = 8.0f;
    float edgeOpacity = 0.7f;
    bool operator==(const LineBandStyle &) const = default;
};

}  // namespace krok::subtitle::native
