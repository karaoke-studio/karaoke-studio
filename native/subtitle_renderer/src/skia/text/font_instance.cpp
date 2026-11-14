#include "text/font_instance.h"
#include "include/core/SkData.h"
#include "include/core/SkFontArguments.h"
#include "include/core/SkFontMetrics.h"
#include "include/core/SkFontTypes.h"
#include "include/core/SkFontMgr.h"
#include "include/core/SkTypeface.h"
#include "include/ports/SkFontMgr_empty.h"
#include <algorithm>
#include <bit>
#include <cmath>
#include <map>
#include <sstream>
#include <stdexcept>

namespace subtitle::skia::text {
FontInstance::FontInstance(const FontSpec& spec) {
    if (!spec.resource || spec.resource->bytes.empty() || spec.resource->sha256.size() != 64
        || spec.resource->faceIndex < 0 || !std::isfinite(spec.size) || spec.size <= 0 || spec.size > 2048
        || !std::isfinite(spec.scaleX) || spec.scaleX < .1f || spec.scaleX > 8)
        throw std::runtime_error("Skia 字体资源、字号或水平缩放无效");
    const auto& source = *spec.resource;
    auto manager = SkFontMgr_New_Custom_Empty();
    auto face = manager->makeFromData(SkData::MakeWithCopy(source.bytes.data(), source.bytes.size()), source.faceIndex);
    if (!face) throw std::runtime_error("Skia 无法读取指定字体 face");
    const int count = face->getVariationDesignParameters({});
    if (count < 0) throw std::runtime_error("Skia 无法读取字体轴信息");
    std::vector<SkFontParameters::Variation::Axis> parameters(count);
    if (count && face->getVariationDesignParameters(parameters) != count)
        throw std::runtime_error("Skia 字体轴信息不完整");
    std::map<std::uint32_t, float> requested;
    for (const auto& axis : source.axes) {
        if (!std::isfinite(axis.value) || !requested.emplace(axis.tag, axis.value).second)
            throw std::runtime_error("Skia 字体轴重复或包含非有限值");
    }
    std::vector<SkFontArguments::VariationPosition::Coordinate> coordinates;
    for (const auto& parameter : parameters) {
        const auto found = requested.find(parameter.tag);
        const float value = found == requested.end() ? parameter.def : found->second;
        if (value < parameter.min || value > parameter.max)
            throw std::runtime_error("Skia 字体轴值超出实际 face 范围");
        coordinates.push_back({parameter.tag, value});
        requested.erase(parameter.tag);
    }
    if (!requested.empty()) throw std::runtime_error("Skia 字体 face 不包含请求的轴");
    std::sort(coordinates.begin(), coordinates.end(), [](auto a, auto b) { return a.axis < b.axis; });
    if (!coordinates.empty()) {
        SkFontArguments args;
        args.setCollectionIndex(source.faceIndex);
        args.setVariationDesignPosition({coordinates.data(), int(coordinates.size())});
        face = face->makeClone(args);
        if (!face) throw std::runtime_error("Skia 无法实例化指定字体轴");
    }
    // m150 HB's advance callback also multiplies SkFont::scaleX. Shape at unit
    // scale, then transform geometry AND advances once in our shared result.
    scaleX_ = spec.scaleX;
    font_ = SkFont(face, spec.size, 1, spec.syntheticItalic ? -.25f : 0.f);
    font_.setEmbolden(spec.syntheticBold);
    font_.setHinting(SkFontHinting::kNone);
    font_.setEdging(SkFont::Edging::kAntiAlias);
    font_.setSubpixel(true);
    font_.setLinearMetrics(true);
    SkFontMetrics metrics;
    font_.getMetrics(&metrics);
    if (!std::isfinite(metrics.fAscent) || !std::isfinite(metrics.fDescent) || !std::isfinite(metrics.fLeading))
        throw std::runtime_error("Skia 字体包含非有限基线度量");
    // Float bit patterns avoid locale/decimal rounding collisions in metric cache keys.
    std::ostringstream key;
    key << "skia-m150-hb-ft-v1:" << source.sha256 << ':' << source.faceIndex << ':' << std::bit_cast<std::uint32_t>(spec.size)
        << ':' << std::bit_cast<std::uint32_t>(spec.scaleX) << ':' << spec.syntheticBold << ':' << spec.syntheticItalic;
    for (const auto& axis : coordinates) key << ':' << axis.axis << '=' << std::bit_cast<std::uint32_t>(axis.value);
    identity_ = key.str();
}
float FontInstance::ascent() const { SkFontMetrics m; font_.getMetrics(&m); return -m.fAscent; }
float FontInstance::descent() const { SkFontMetrics m; font_.getMetrics(&m); return m.fDescent; }
float FontInstance::leading() const { SkFontMetrics m; font_.getMetrics(&m); return m.fLeading; }
}
