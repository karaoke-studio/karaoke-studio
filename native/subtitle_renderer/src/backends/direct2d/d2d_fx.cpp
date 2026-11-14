#include "d2d_fx.h"
namespace krok::subtitle::native::direct2d {
D2D1_MATRIX_3X2_F geoCharMatrix(
    const effects::GeoCharState &state,
    float centerX,
    float centerY
) {
    return D2D1::Matrix3x2F::Translation(-centerX, -centerY)
        * D2D1::Matrix3x2F::Scale(state.scaleX, state.scaleY)
        * D2D1::Matrix3x2F::Rotation(state.rotation)
        * D2D1::Matrix3x2F::Translation(
            centerX + state.dx, centerY + state.dy
        );
}

}
