#include "qt_character_animation.h"

#include "qt_display_plan.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;
using protocol::TimingChar;
using protocol::TimingLine;

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr int kUtopiaIntroTimeMs = 700;
constexpr int kUtopiaIntroDelayMs = 200;
constexpr int kUtopiaIntroEnlargeMs = 400;
constexpr int kUtopiaIntroCondenseMs = 100;
constexpr double kUtopiaIntroOverRatio = 1.3;
constexpr double kUtopiaWipeOverRatio = 1.15;
constexpr double kUtopiaWipeOverTimeRatio = 0.25;
constexpr int kUtopiaWipeOverTimeLimitMs = 100;
constexpr int kUtopiaFadeOutTimeMs = 750;
// 整字放大（zoom_pulse）：与 Python transitions.zoom_pulse_wipe_scale 同曲线
// ——唱字期间三次缓出放大到 1.25，唱字结束后 300ms 三次缓入缩回。
constexpr double kZoomPulsePeakRatio = 1.25;
constexpr int kZoomPulseShrinkMs = 300;

}  // namespace

int charEndMs(const TimingLine &line, std::size_t index) {
    if (index >= line.chars.size()) {
        return 0;
    }
    const TimingChar &ch = line.chars[index];
    if (ch.resolvedEndMs.has_value()) {
        return std::max(ch.startMs, ch.resolvedEndMs.value());
    }
    if (ch.pauseReleaseMs.has_value()) {
        return std::max(ch.startMs, ch.pauseReleaseMs.value());
    }
    if (index + 1 < line.chars.size()) {
        return std::max(ch.startMs, line.chars[index + 1].startMs);
    }
    if (line.endMs > ch.startMs) {
        return line.endMs;
    }
    return ch.startMs + 1;
}

std::vector<std::pair<int, int>> lineIntervals(const TimingLine &line) {
    std::vector<std::pair<int, int>> intervals;
    intervals.reserve(line.chars.size());
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        intervals.push_back({line.chars[i].startMs, charEndMs(line, i)});
    }
    return intervals;
}

double progressRatio(int startMs, int endMs, int tMs) {
    if (endMs <= startMs) {
        return tMs >= startMs ? 1.0 : 0.0;
    }
    const double raw = static_cast<double>(tMs - startMs) / static_cast<double>(endMs - startMs);
    return std::clamp(raw, 0.0, 1.0);
}

double characterFillRatio(
    const std::vector<std::pair<int, int>> &intervals,
    std::size_t index,
    int tMs
) {
    if (index >= intervals.size()) {
        return 0.0;
    }
    const auto interval = intervals[index];
    if (tMs < interval.first) {
        return 0.0;
    }
    if (tMs >= interval.second) {
        return 1.0;
    }
    return progressRatio(interval.first, interval.second, tMs);
}

int lineDisplayEndMs(const TimingLine &line, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return 0;
    }
    return std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
}

int nextValidCharIndex(const TimingLine &line, std::size_t startIndex) {
    for (std::size_t i = startIndex; i < line.chars.size(); ++i) {
        if (!line.chars[i].text.trimmed().isEmpty()) {
            return static_cast<int>(i);
        }
    }
    return -1;
}

int utopiaTailDelayMs(const RenderConfig &cfg) {
    return std::max(0, cfg.lineTailMs - kUtopiaFadeOutTimeMs);
}

int utopiaFollowingDoneTime(
    const TimingLine &line,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    const RenderConfig &cfg
) {
    if (intervals.empty()) {
        return line.endMs;
    }
    index = std::clamp(index, 0, static_cast<int>(intervals.size()) - 1);
    const int currentEnd = intervals[static_cast<std::size_t>(index)].second;
    const int nextIndex = nextValidCharIndex(line, static_cast<std::size_t>(index + 1));
    if (nextIndex >= 0 && nextIndex < static_cast<int>(intervals.size())) {
        const int nextEnd = intervals[static_cast<std::size_t>(nextIndex)].second;
        if (currentEnd <= nextEnd) {
            return nextEnd;
        }
    }
    return currentEnd + utopiaTailDelayMs(cfg);
}

bool isUtopiaWiping(int tMs, int charStartMs, int charEndMs) {
    return charStartMs < tMs && tMs < charEndMs && charStartMs != charEndMs;
}

bool isZoomPulseActive(int tMs, int charStartMs, int charEndMs) {
    return charStartMs != charEndMs
        && charStartMs < tMs
        && tMs < charEndMs + kZoomPulseShrinkMs;
}

double zoomPulseScale(int tMs, int charStartMs, int charEndMs, int curveLevel) {
    if (!isZoomPulseActive(tMs, charStartMs, charEndMs)) {
        return 1.0;
    }
    const int level = std::clamp(curveLevel, 0, 5);
    if (tMs < charEndMs) {
        // 缓出：起点快速离开 1.0，逼近峰值时导数→0（峰值停留）。
        const double one = 1.0
            - static_cast<double>(tMs - charStartMs)
                / static_cast<double>(charEndMs - charStartMs);
        double powered = one;
        for (int i = 1; i < level; ++i) {
            powered *= one;
        }
        const double eased = level <= 0 ? 1.0 - one : 1.0 - powered;
        return 1.0 + (kZoomPulsePeakRatio - 1.0) * eased;
    }
    // 缓入：刚唱完时导数≈0（继续停留在峰值附近），结尾快速落回 1.0。
    const double q = static_cast<double>(tMs - charEndMs) / kZoomPulseShrinkMs;
    double powered = q;
    for (int i = 1; i < level; ++i) {
        powered *= q;
    }
    const double eased = level <= 0 ? 1.0 - q : 1.0 - powered;
    return 1.0 + (kZoomPulsePeakRatio - 1.0) * eased;
}

double utopiaWipeScale(int tMs, int charStartMs, int charEndMs) {
    if (!isUtopiaWiping(tMs, charStartMs, charEndMs)) {
        return 1.0;
    }
    const int overMs = std::min(
        static_cast<int>((charEndMs - charStartMs) * kUtopiaWipeOverTimeRatio),
        kUtopiaWipeOverTimeLimitMs
    );
    if (overMs <= 0) {
        return 1.0;
    }
    const int peakMs = charStartMs + overMs;
    double progress = 0.0;
    if (tMs <= peakMs) {
        progress = static_cast<double>(tMs - charStartMs) / overMs;
    } else {
        const int releaseMs = std::max(charEndMs - peakMs, 1);
        progress = static_cast<double>(charEndMs - tMs) / releaseMs;
    }
    return 1.0 + (kUtopiaWipeOverRatio - 1.0) * std::clamp(progress, 0.0, 1.0);
}

bool utopiaKaraokeEnabled(const RenderConfig &cfg) {
    if (cfg.karaokeAnim == QStringLiteral("utopia")) {
        return true;
    }
    if (cfg.karaokeAnim == QStringLiteral("none")) {
        return false;
    }
    return cfg.entryAnim == QStringLiteral("utopia")
        || cfg.exitAnim == QStringLiteral("utopia");
}

std::optional<LineCharTransition> lineCharTransitionContext(
    const RenderConfig &cfg,
    const TimingLine &line,
    int tMs,
    const std::vector<std::pair<int, int>> &intervals,
    bool zoomPulseEnabled
) {
    if (line.chars.empty()) {
        return std::nullopt;
    }
    if (cfg.entryAnim != QStringLiteral("utopia")
        && cfg.exitAnim != QStringLiteral("utopia")
        && !utopiaKaraokeEnabled(cfg)) {
        return std::nullopt;
    }

    const int start = lineStartMs(line);
    const int end = lineDisplayEndMs(line, cfg);
    const bool inIntro = cfg.entryAnim == QStringLiteral("utopia") && tMs <= start + kUtopiaIntroTimeMs;
    const bool inExit = cfg.exitAnim == QStringLiteral("utopia")
        && !intervals.empty()
        && utopiaFollowingDoneTime(line, intervals, 0, cfg) <= tMs
        && tMs <= end;
    bool inWipe = false;
    if (utopiaKaraokeEnabled(cfg)) {
        for (const auto &interval : intervals) {
            // 整字放大的缩小尾窗延伸到唱字结束后 300ms：行尾字还在缩回时
            // 即便没有别的字符在唱，transition 也必须存在，否则字形跳回原位。
            const bool active = zoomPulseEnabled
                ? isZoomPulseActive(tMs, interval.first, interval.second)
                : isUtopiaWiping(tMs, interval.first, interval.second);
            if (active) {
                inWipe = true;
                break;
            }
        }
    }
    if (!inIntro && !inExit && !inWipe) {
        return std::nullopt;
    }

    return LineCharTransition{
        QStringLiteral("utopia"),
        QStringLiteral("utopia"),
        1.0,
        start,
        end,
    };
}

QTransform characterTransform(
    double centerX,
    double centerY,
    const AnimationState &state,
    std::optional<QPointF> scaleOrigin
) {
    QTransform transform;
    if (state.dx == 0.0
        && state.dy == 0.0
        && state.rotation == 0.0
        && state.scaleX == 1.0
        && state.scaleY == 1.0
        && state.skewY == 0.0) {
        return transform;
    }
    if (scaleOrigin.has_value()) {
        transform.translate(scaleOrigin->x() + state.dx, scaleOrigin->y() + state.dy);
        if (state.skewY != 0.0) {
            transform.shear(0.0, state.skewY);
        }
        if (state.scaleX != 1.0 || state.scaleY != 1.0) {
            transform.scale(state.scaleX, state.scaleY);
        }
        transform.translate(centerX - scaleOrigin->x(), centerY - scaleOrigin->y());
        if (state.rotation != 0.0) {
            transform.rotate(state.rotation);
        }
        transform.translate(-centerX, -centerY);
        return transform;
    }
    transform.translate(centerX + state.dx, centerY + state.dy);
    if (state.rotation != 0.0) {
        transform.rotate(state.rotation);
    }
    if (state.skewY != 0.0) {
        transform.shear(0.0, state.skewY);
    }
    if (state.scaleX != 1.0 || state.scaleY != 1.0) {
        transform.scale(state.scaleX, state.scaleY);
    }
    transform.translate(-centerX, -centerY);
    return transform;
}

AnimationState transitionCharState(
    const RenderConfig &cfg,
    const LineCharTransition &transition,
    const std::vector<std::pair<int, int>> &intervals,
    int index,
    int count,
    int tMs,
    int frameHeight,
    int followingDoneMs,
    bool zoomPulseEnabled,
    int zoomPulseCurveLevel,
    std::optional<std::pair<int, int>> overrideInterval
) {
    if (transition.effect == QStringLiteral("utopia") && transition.phase == QStringLiteral("utopia")) {
        if (cfg.entryAnim == QStringLiteral("utopia") && tMs <= transition.startMs + kUtopiaIntroTimeMs) {
            const int delay = count <= 1 ? 0 : kUtopiaIntroDelayMs / (count - 1) * index;
            const int elapsed = tMs - transition.startMs - delay;
            if (elapsed < 0) {
                return AnimationState{0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
            }
            const double opacity = std::min(static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs, 1.0);
            double scale = 1.0;
            if (elapsed < kUtopiaIntroEnlargeMs) {
                scale = kUtopiaIntroOverRatio * static_cast<double>(elapsed) / kUtopiaIntroEnlargeMs;
            } else if (elapsed < kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs) {
                const int remaining = kUtopiaIntroEnlargeMs + kUtopiaIntroCondenseMs - elapsed;
                scale = 1.0 + (kUtopiaIntroOverRatio - 1.0) * static_cast<double>(remaining) / kUtopiaIntroCondenseMs;
            }
            return AnimationState{opacity, 0.0, 0.0, 0.0, scale, scale, 0.0};
        }

        // 整字放大与 utopia 退场并行：退场照原时间开始，缩回缩放继续
        // 乘在退场自身的缩放/翻转上（镜像 transitions.py 的 exit 合成）。
        double zoomOverlay = 1.0;
        if (zoomPulseEnabled) {
            if (overrideInterval.has_value()) {
                zoomOverlay = zoomPulseScale(
                    tMs, overrideInterval->first, overrideInterval->second,
                    zoomPulseCurveLevel
                );
            } else if (index >= 0
                && index < static_cast<int>(intervals.size())) {
                const auto interval =
                    intervals[static_cast<std::size_t>(index)];
                zoomOverlay = zoomPulseScale(
                    tMs, interval.first, interval.second, zoomPulseCurveLevel
                );
            }
        }
        if (cfg.exitAnim == QStringLiteral("utopia")
            && tMs > followingDoneMs) {
            double local = static_cast<double>(tMs - followingDoneMs) / kUtopiaFadeOutTimeMs;
            local = std::clamp(local, 0.0, 1.0);
            const double opacity = std::max(0.0, 1.0 - local);
            const double shrink = 1.0 - local;
            const double amp = std::max(frameHeight, 1) / 15.0;
            const double xTravel = local <= 0.5
                ? std::sin(kPi * local) * amp
                : amp + std::sin((local - 0.5) * kPi) * amp;
            const double yTravel = std::sin(kPi * local / 2.0) * amp;
            const double xFlip = std::cos(kPi * local);
            return AnimationState{
                opacity,
                -xTravel,
                yTravel,
                -180.0 * local,
                shrink * xFlip * zoomOverlay,
                shrink * zoomOverlay,
                0.0,
            };
        }

        if (utopiaKaraokeEnabled(cfg)
            && (overrideInterval.has_value()
                || (index >= 0 && index < static_cast<int>(intervals.size())))) {
            const auto interval = overrideInterval.has_value()
                ? overrideInterval.value()
                : intervals[static_cast<std::size_t>(index)];
            const bool active = zoomPulseEnabled
                ? isZoomPulseActive(tMs, interval.first, interval.second)
                : isUtopiaWiping(tMs, interval.first, interval.second);
            if (active) {
                const double scale = zoomPulseEnabled
                    ? zoomPulseScale(
                        tMs, interval.first, interval.second, zoomPulseCurveLevel
                    )
                    : utopiaWipeScale(tMs, interval.first, interval.second);
                return AnimationState{1.0, 0.0, 0.0, 0.0, scale, scale, 0.0};
            }
        }
    }

    return AnimationState{};
}

}  // namespace krok::subtitle::native::legacy_qt
