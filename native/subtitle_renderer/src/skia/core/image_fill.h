#pragma once

#include "include/core/SkImage.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkSamplingOptions.h"
#include "include/core/SkShader.h"
#include "include/core/SkTileMode.h"
#include <algorithm>
#include <array>

namespace subtitle::skia {
// A static image paint as the existing GPU draws it: the decoded image repeats
// in both axes with linear sampling (no mipmaps), anchored at the device
// origin, scaled by imageScale at the layout reference scale (clamped to
// 0.01..10). The decoder lives at the platform boundary; this is pixels only.
struct ImageFill {
    sk_sp<SkImage> image;
    float scale = 1;

    explicit operator bool() const { return bool(image); }

    static float scaleFor(float imageScale, float layoutScale) {
        return std::clamp(imageScale * std::max(layoutScale, .01f), .01f, 10.f);
    }

    // The tiles stay on the device grid wherever the line is translated.
    sk_sp<SkShader> shaderAt(const SkMatrix& totalMatrix) const {
        SkMatrix local;
        if (!image || !totalMatrix.invert(&local)) return nullptr;
        local.preScale(scale, scale);
        return image->makeShader(SkTileMode::kRepeat, SkTileMode::kRepeat,
                                 SkSamplingOptions(SkFilterMode::kLinear, SkMipmapMode::kNone), &local);
    }
};

// Before/after image paints of each drawn layer; empty slots use the
// gradient or solid paint of the same layer.
struct LineImageFills {
    ImageFill body[2], outline[2], outer[2];
};

struct BodyPaint {
    std::array<sk_sp<SkShader>, 2> gradients;
    std::array<ImageFill, 2> images;
    std::array<sk_sp<SkShader>, 2> outlineGradients, outerGradients;
};
}
