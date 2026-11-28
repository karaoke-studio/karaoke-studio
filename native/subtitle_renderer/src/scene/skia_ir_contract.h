#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>

namespace krok::subtitle::native::scene {
// The sidecar and its IR builder ship from one source tree. Any change to the
// IR, the measurement protocol or the frame protocol bumps this one version.
inline constexpr int kSkiaProtocolVersion = 6;
inline constexpr int kScanlineBandSchema = 6;
inline constexpr int kCharacterTransformSchema = 8;
inline constexpr int kSingingParticleSchema = 13;

// Render modes the IR builder selected for this scene. The sidecar follows
// them and never refuses a scene over their combination: the sidecar and the
// IR builder ship from one source tree, so there is nothing to negotiate.
QJsonArray skiaFeatures(const QJsonObject& ir);
bool hasSkiaVerticalParticleProfile(const QJsonArray& features);
bool isSkiaFallingParticle(const QString& kind);
QString skiaParticleKindForTransition(const QString& animation, bool entry);
bool allowsSkiaRubyParticleTransition(const QString& entryKind, const QString& exitKind,
                                      const QJsonArray& features);
// flame/lightning entry and exit (character_transform_schema>=8, sing_twinkle_schema>=11).
bool isSkiaEnergyTransition(const QString& animation);
// Whether the planner emits this burst kind as a layer of the given transition kind.
bool isSkiaTransitionLayer(const QString& transition, const QString& kind, bool entry);
void validateSkiaIrEnvelope(const QJsonObject& ir);
}
