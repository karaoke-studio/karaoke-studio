#pragma once
#include "include/core/SkPath.h"
#include <cstdint>
#include <vector>

class SkCanvas;
namespace subtitle::skia {
// P04's deliberately small input contract. Full shaping/metrics are a P05 gate.
// Neither device handles, Qt objects nor filesystem paths enter the drawing core.
struct SubtitleCell {
    std::uint32_t codepoint = 0;
    int startMs = 0;
    int endMs = 0;
};
struct SubtitleLine {
    std::vector<std::uint8_t> fontBytes;
    std::vector<SubtitleCell> cells;
    float fontSize = 40;
    float letterSpacing = 0;
    float spaceWidth = 8;
    float centerX = 0;
    float bottomY = 0;
    std::uint32_t beforeArgb = 0xffffffff;
    std::uint32_t afterArgb = 0xffff5a6f;
    int visibleStartMs = 0;
    int visibleEndMs = 0;
};
struct SubtitleInput {
    int width = 0;
    int height = 0;
    std::vector<SubtitleLine> lines;
};
class PreparedScene {
public:
    explicit PreparedScene(const SubtitleInput& input);
    void draw(SkCanvas& canvas, int tMs) const;
    int width() const { return width_; }
    int height() const { return height_; }
private:
    struct Cell { SkPath path; float left, right; int startMs, endMs; };
    struct Line {
        std::vector<Cell> cells;
        std::uint32_t before, after;
        int startMs, endMs;
    };
    int width_, height_;
    std::vector<Line> lines_;
};
}
