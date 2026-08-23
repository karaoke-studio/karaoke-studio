#include "d2d_opacity_layer.h"

#include <d2d1helper.h>

#include <utility>

namespace krok::subtitle::native::direct2d {

OpacityLayerScope::~OpacityLayerScope() {
    pop();
}

bool OpacityLayerScope::prepare(ID2D1DeviceContext *context, float opacity) {
    if (context == nullptr) {
        return false;
    }
    Microsoft::WRL::ComPtr<ID2D1Layer> layer;
    if (FAILED(context->CreateLayer(nullptr, &layer)) || !layer) {
        return false;
    }
    context_ = context;
    layer_ = std::move(layer);
    opacity_ = opacity;
    return true;
}

void OpacityLayerScope::push() {
    if (context_ == nullptr || pushed_) {
        return;
    }
    context_->PushLayer(
        D2D1::LayerParameters(
            D2D1::InfiniteRect(),
            nullptr,
            D2D1_ANTIALIAS_MODE_PER_PRIMITIVE,
            D2D1::IdentityMatrix(),
            opacity_
        ),
        layer_.Get()
    );
    pushed_ = true;
}

void OpacityLayerScope::pop() {
    if (context_ != nullptr && pushed_) {
        context_->PopLayer();
        pushed_ = false;
    }
}

bool OpacityLayerScope::prepared() const {
    return context_ != nullptr && !pushed_;
}

}  // namespace krok::subtitle::native::direct2d
