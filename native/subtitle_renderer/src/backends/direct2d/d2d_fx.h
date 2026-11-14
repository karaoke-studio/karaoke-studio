#pragma once
#include "../../effects/geometry.h"
#include "../../effects/particles.h"
#include "../../effects/noise.h"
#include "../../effects/scanline_column.h"
#include "../../effects/scanline_image.h"
#include "../../effects/effect_constants.h"
#include "../../effects/unit_hash.h"
#include <d2d1_2.h>
namespace krok::subtitle::native::direct2d {
D2D1_MATRIX_3X2_F geoCharMatrix(const effects::GeoCharState&, float centerX, float centerY);
}
