#pragma once

#include "text_line.h"
#include "text_style.h"
#include "line_band.h"

namespace krok::subtitle::native {

struct RenderScene {
    int width = 1920;
    int height = 1080;
    // Raster scale used by preview targets.  Layout keeps the output-resolution
    // integer semantics and scales the finished metrics by this factor; export
    // remains on the existing 1.0 path.
    float layoutReferenceScale = 1.0f;
    int exportCropTop = 0;
    int exportCropHeight = 0;
    std::vector<std::pair<int, int>> exportBands;
    int prewarmTimeMs = 0;
    bool realizationEnabled = true;
    bool deferRealizationPrewarmUntilFirstFrame = false;
    std::uint64_t realizationCapacity = 8192;
    float viewportScale = 1.0f;
    float viewportRotation = 0.0f;
    float viewportOffsetX = 0.0f;
    float viewportOffsetY = 0.0f;
    std::string viewportAlign = "center";
    TextStyle style;
    std::vector<TextStyle> lineStyles;
    std::vector<TextStyle> charStyles;
    // 指示灯/音量柱 ``role`` 外观档（复用配色方案）的固定装饰源：方案
    // 叠加到全局 base 后投影出的完整 TextStyle（镜像 Painter 的
    // appearance_role_source）。仅在对应 appearanceMode == "role" 且来源
    // 名可解析时存在；悬空时 nullopt，渲染端回退 auto 口径（段首行第一
    // 个角色）。
    std::optional<TextStyle> litDecorStyle;
    std::optional<TextStyle> volumeDecorStyle;
    // 行背景带（全局装饰层，不参与布局/碰撞）。见 LineBandStyle。
    LineBandStyle lineBand;
    std::vector<TextLine> lines;
    // 装饰粒子 sprite 轮廓表（scene IR ``fx_sprites``；Python 单一事实源，
    // 常量内容、值语义参与相等性比较）。
    std::vector<std::pair<std::string, VectorGlyph>> fxSprites;
    bool operator==(const RenderScene &) const = default;
};

}  // namespace krok::subtitle::native
