#pragma once

// 矢量字形描边的 Clipper2 预展开（2026-10 方案二）：
// CreateStrokedGeometryRealization 对「密集路径 × 宽描边」是平方级 CPU
// 爆炸（实测 3847 段 × 15.6px = 49s，与段数/宽度平方成正比），而多边形
// 圆角偏置是线性扫描线算法（同输入 7ms）。这里把闭合填充路径预展开成
// 「描边区域」的填充轮廓，之后描边一律按 FILL 语义烘焙/绘制（填充
// realization 是线性的），对任意病态 SVG 免疫。
// 观感差异经独立工具在 8 个真实导唱符上量化：平均边缘偏移
// 0.17~0.30 源像素/侧（亚像素，含 7781 段样本）。

#include "d2d_device.h"

#include <d2d1_2.h>
#include <wrl/client.h>
#include <memory>

namespace krok::subtitle::native::direct2d {

struct StrokeOutlineCache;

// 把闭合填充路径按 strokeWidth/2 圆角偏置成描边区域轮廓。
// source 必须是 ID2D1PathGeometry（矢量字形 LRU 的 resource.path）。
// 失败（非 path / 空路径 / 偏置结果为空）返回 nullptr，调用方回落原
// 描边路径。返回 WINDING 轮廓（与产品消费矢量字形的填充规则同口径，
// 不做 evenodd 归一）。
Microsoft::WRL::ComPtr<ID2D1PathGeometry> strokeOutlineGeometry(
    const D2DDevice &device,
    ID2D1Geometry *source,
    float strokeWidth,
    bool outsideOnly = false,
    float innerStrokeWidth = 0.0f,
    std::shared_ptr<StrokeOutlineCache> *cache = nullptr
);

}  // namespace krok::subtitle::native::direct2d
