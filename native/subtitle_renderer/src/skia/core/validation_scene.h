#pragma once

#include "include/core/SkRefCnt.h"
#include <filesystem>

class SkCanvas;
class SkImage;
class SkShader;
class SkTypeface;

namespace subtitle::skia {
// P01 capability scene, shared verbatim by GPU and Raster. Subtitle IR comes in P02+.
class ValidationScene {
public:
    explicit ValidationScene(const std::filesystem::path& fontFile);
    ~ValidationScene();
    void draw(SkCanvas& canvas) const;
    static constexpr int width = 640;
    static constexpr int height = 360;
private:
    sk_sp<SkTypeface> typeface_;
    sk_sp<SkImage> image_;
    sk_sp<SkShader> shader_;
};
}
