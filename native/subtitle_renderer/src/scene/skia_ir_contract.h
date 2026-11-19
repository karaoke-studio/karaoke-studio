#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>

namespace krok::subtitle::native::scene {
inline constexpr int kScanlineBandSchema = 4;
inline constexpr int kCharacterTransformSchema = 6;
inline constexpr int kSingingParticleSchema = 9;

const QJsonArray& skiaIrProfiles();
bool isSkiaFallingParticle(const QString& kind);
void validateSkiaIrEnvelope(const QJsonObject& ir);
}
