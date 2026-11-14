#pragma once
#include "include/core/SkSurface.h"

namespace subtitle::skia {
sk_sp<SkSurface> makeRasterSurface(int width, int height);
}
