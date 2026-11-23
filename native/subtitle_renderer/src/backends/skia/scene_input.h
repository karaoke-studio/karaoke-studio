#pragma once
#include "../../model/render_scene.h"
#include "core/prepared_scene.h"
namespace krok::subtitle::native {
enum class GlowPaintSupport { Solid, VerticalGradient, LineGradients };

struct SkiaPaintCapabilities {
    int outlineLayers = 0;
    bool bodyGradients = false;
    bool outlineGradients = false;
    bool imageFills = false;
    bool staticShadow = false;
    bool staticGlow = false;
    bool customShapeSignal = false;
    bool imageShapeSignal = false;
    bool decoratedShapeSignal = false;
    bool vectorShapeSignal = false;
    bool volumeSignal = false;
    bool staticLineBand = false;
    bool roleOutlines = false;
    bool roleBodyPaints = false;
    bool roleStaticShadow = false;
    bool roleStaticGlow = false;
    bool bodyShadowGradients = false;
    bool bodyShadowImages = false;
    GlowPaintSupport glowPaints = GlowPaintSupport::Solid;
};
bool supportsSkiaGlowPaint(const PaintStyle&, GlowPaintSupport);
bool supportsSkiaShadowPaint(const PaintStyle&, bool gradients);
bool hasVisibleGradientStops(const PaintStyle&);
bool usesFadedUtopiaScanline(const TextLine&, const TextStyle&);
void validateSkiaScene(const RenderScene&, bool canonical = false, SkiaPaintCapabilities = {});
SkMatrix skiaViewport(const RenderScene&);
::subtitle::skia::SubtitleInput skiaSceneInput(const RenderScene& scene);
::subtitle::skia::StaticShadow skiaStaticShadow(const TextStyle& style);
::subtitle::skia::StaticGlow skiaStaticGlow(const TextStyle& style);
}
