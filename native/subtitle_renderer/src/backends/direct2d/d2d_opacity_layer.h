#pragma once

#include <d2d1_2.h>
#include <wrl/client.h>

namespace krok::subtitle::native::direct2d {

class OpacityLayerScope {
public:
    OpacityLayerScope() = default;
    OpacityLayerScope(const OpacityLayerScope &) = delete;
    OpacityLayerScope &operator=(const OpacityLayerScope &) = delete;
    ~OpacityLayerScope();

    bool prepare(ID2D1DeviceContext *context, float opacity);
    void push();
    void pop();
    bool prepared() const;

private:
    ID2D1DeviceContext *context_ = nullptr;
    Microsoft::WRL::ComPtr<ID2D1Layer> layer_;
    float opacity_ = 1.0f;
    bool pushed_ = false;
};

}  // namespace krok::subtitle::native::direct2d
