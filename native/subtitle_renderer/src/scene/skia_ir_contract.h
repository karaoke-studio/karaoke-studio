#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>

namespace krok::subtitle::native::scene {
inline constexpr int kScanlineBandSchema = 6;
inline constexpr int kCharacterTransformSchema = 8;
inline constexpr int kSingingParticleSchema = 13;

const QJsonArray& skiaIrProfiles();
bool isSkiaFallingParticle(const QString& kind);
QString skiaParticleKindForTransition(const QString& animation, bool entry);
// flame/lightning entry and exit (character_transform_schema>=8, sing_twinkle_schema>=11).
bool isSkiaEnergyTransition(const QString& animation);
// Whether the planner emits this burst kind as a layer of the given transition kind.
bool isSkiaTransitionLayer(const QString& transition, const QString& kind, bool entry);
void validateSkiaIrEnvelope(const QJsonObject& ir);
}
