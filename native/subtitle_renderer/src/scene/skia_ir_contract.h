#pragma once

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>

namespace krok::subtitle::native::scene {
inline constexpr int kScanlineBandSchema = 4;
inline constexpr int kCharacterTransformSchema = 5;
inline constexpr int kSingingParticleSchema = 7;

const QJsonArray& skiaIrProfiles();
void validateSkiaIrEnvelope(const QJsonObject& ir);
}
