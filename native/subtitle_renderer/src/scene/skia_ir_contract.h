#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>

namespace krok::subtitle::native::scene {
inline constexpr int kScanlineBandSchema = 4;
inline constexpr int kCharacterTransformSchema = 7;
inline constexpr int kSingingParticleSchema = 10;

const QJsonArray& skiaIrProfiles();
bool isSkiaFallingParticle(const QString& kind);
QString skiaParticleKindForTransition(const QString& animation, bool entry);
void validateSkiaIrEnvelope(const QJsonObject& ir);
}
