#include <QtCore/QElapsedTimer>
#include <QtCore/QPointF>
#include <QtCore/QRectF>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtGui/QBrush>
#include <QtGui/QColor>
#include <QtGui/QFont>
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtGui/QLinearGradient>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QPen>
#include <QtGui/QTransform>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <numeric>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#endif

namespace {

struct Options {
    int width = 2400;
    int height = 1350;
    int frames = 240;
    int fps = 60;
    int fontSize = 80;
    int rubySize = 32;
    int runs = 3;
    bool glow = true;
    bool ruby = true;
    bool utopia = true;
    std::vector<int> threadLevels{1, 2, 4, 8, 16};
};

struct RenderStats {
    int threads = 1;
    int run = 1;
    double wallSeconds = 0.0;
    double fps = 0.0;
    double msPerFrame = 0.0;
    double cpuCoresAvg = std::numeric_limits<double>::quiet_NaN();
    double cpuCoresMax = std::numeric_limits<double>::quiet_NaN();
    double cpuAllCorePctAvg = std::numeric_limits<double>::quiet_NaN();
    double cpuAllCorePctMax = std::numeric_limits<double>::quiet_NaN();
    std::uint64_t checksum = 0;
};

struct SummaryStats {
    int threads = 1;
    int runs = 1;
    double wallSecondsMedian = 0.0;
    double fpsMedian = 0.0;
    double msPerFrameMedian = 0.0;
    double speedupMedian = 1.0;
    double cpuCoresAvgMedian = std::numeric_limits<double>::quiet_NaN();
    double cpuAllCorePctAvgMedian = std::numeric_limits<double>::quiet_NaN();
    double cpuCoresMaxObserved = std::numeric_limits<double>::quiet_NaN();
    double cpuAllCorePctMaxObserved = std::numeric_limits<double>::quiet_NaN();
};

QString utf8(const char *text) {
    return QString::fromUtf8(text);
}

double median(std::vector<double> values) {
    values.erase(
        std::remove_if(values.begin(), values.end(), [](double value) { return !std::isfinite(value); }),
        values.end()
    );
    if (values.empty()) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    std::sort(values.begin(), values.end());
    const std::size_t mid = values.size() / 2;
    if (values.size() % 2 == 1) {
        return values[mid];
    }
    return (values[mid - 1] + values[mid]) * 0.5;
}

double maxFinite(const std::vector<double> &values) {
    double result = std::numeric_limits<double>::quiet_NaN();
    for (const double value : values) {
        if (!std::isfinite(value)) {
            continue;
        }
        if (!std::isfinite(result) || value > result) {
            result = value;
        }
    }
    return result;
}

std::string metric(double value, int precision = 1) {
    if (!std::isfinite(value)) {
        return "n/a";
    }
    std::ostringstream ss;
    ss << std::fixed << std::setprecision(precision) << value;
    return ss.str();
}

#ifdef _WIN32
std::uint64_t fileTimeToUInt64(const FILETIME &fileTime) {
    return (static_cast<std::uint64_t>(fileTime.dwHighDateTime) << 32) |
           static_cast<std::uint64_t>(fileTime.dwLowDateTime);
}

double processCpuSeconds() {
    FILETIME creationTime;
    FILETIME exitTime;
    FILETIME kernelTime;
    FILETIME userTime;
    if (!GetProcessTimes(GetCurrentProcess(), &creationTime, &exitTime, &kernelTime, &userTime)) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const std::uint64_t ticks = fileTimeToUInt64(kernelTime) + fileTimeToUInt64(userTime);
    return static_cast<double>(ticks) / 10'000'000.0;
}

int logicalProcessorCount() {
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    return static_cast<int>(std::max<DWORD>(static_cast<DWORD>(1), info.dwNumberOfProcessors));
}
#else
double processCpuSeconds() {
    return std::numeric_limits<double>::quiet_NaN();
}

int logicalProcessorCount() {
    return static_cast<int>(std::max(1u, std::thread::hardware_concurrency()));
}
#endif

class CpuSampler {
public:
    void start() {
        stopRequested_.store(false, std::memory_order_relaxed);
        worker_ = std::thread([this]() { sampleLoop(); });
    }

    void stop() {
        stopRequested_.store(true, std::memory_order_relaxed);
        if (worker_.joinable()) {
            worker_.join();
        }
    }

    double averageCores() const {
        std::lock_guard<std::mutex> lock(mutex_);
        if (coreSamples_.empty()) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        return std::accumulate(coreSamples_.begin(), coreSamples_.end(), 0.0) /
               static_cast<double>(coreSamples_.size());
    }

    double maxCores() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return maxFinite(coreSamples_);
    }

    double averageAllCorePct() const {
        std::lock_guard<std::mutex> lock(mutex_);
        if (pctSamples_.empty()) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        return std::accumulate(pctSamples_.begin(), pctSamples_.end(), 0.0) /
               static_cast<double>(pctSamples_.size());
    }

    double maxAllCorePct() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return maxFinite(pctSamples_);
    }

private:
    void sampleLoop() {
        const int logicalCores = logicalProcessorCount();
        double previousCpu = processCpuSeconds();
        auto previousWall = std::chrono::steady_clock::now();

        while (!stopRequested_.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            const double currentCpu = processCpuSeconds();
            const auto currentWall = std::chrono::steady_clock::now();
            const double wallDelta = std::chrono::duration<double>(currentWall - previousWall).count();
            const double cpuDelta = currentCpu - previousCpu;

            if (std::isfinite(currentCpu) && std::isfinite(previousCpu) && wallDelta > 0.0 && cpuDelta >= 0.0) {
                const double coreEquivalent = cpuDelta / wallDelta;
                const double allCorePct = coreEquivalent * 100.0 / static_cast<double>(logicalCores);
                std::lock_guard<std::mutex> lock(mutex_);
                coreSamples_.push_back(coreEquivalent);
                pctSamples_.push_back(allCorePct);
            }

            previousCpu = currentCpu;
            previousWall = currentWall;
        }
    }

    std::atomic<bool> stopRequested_{false};
    std::thread worker_;
    mutable std::mutex mutex_;
    std::vector<double> coreSamples_;
    std::vector<double> pctSamples_;
};

std::vector<int> parseThreadLevels(const std::string &value) {
    std::vector<int> result;
    std::stringstream ss(value);
    std::string item;
    while (std::getline(ss, item, ',')) {
        if (item.empty()) {
            continue;
        }
        result.push_back(std::max(1, std::atoi(item.c_str())));
    }
    if (result.empty()) {
        result.push_back(1);
    }
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
}

Options parseOptions(int argc, char **argv) {
    Options opt;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto nextValue = [&](const char *name) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << name << "\n";
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--width") {
            opt.width = std::max(1, std::atoi(nextValue("--width").c_str()));
        } else if (arg == "--height") {
            opt.height = std::max(1, std::atoi(nextValue("--height").c_str()));
        } else if (arg == "--frames") {
            opt.frames = std::max(1, std::atoi(nextValue("--frames").c_str()));
        } else if (arg == "--fps") {
            opt.fps = std::max(1, std::atoi(nextValue("--fps").c_str()));
        } else if (arg == "--runs") {
            opt.runs = std::max(1, std::atoi(nextValue("--runs").c_str()));
        } else if (arg == "--font-size") {
            opt.fontSize = std::max(1, std::atoi(nextValue("--font-size").c_str()));
        } else if (arg == "--ruby-size") {
            opt.rubySize = std::max(1, std::atoi(nextValue("--ruby-size").c_str()));
        } else if (arg == "--threads") {
            opt.threadLevels = parseThreadLevels(nextValue("--threads"));
        } else if (arg == "--no-glow") {
            opt.glow = false;
        } else if (arg == "--no-ruby") {
            opt.ruby = false;
        } else if (arg == "--no-utopia") {
            opt.utopia = false;
        } else if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: krok_qpainter_parallel_probe [options]\n"
                << "  --width N          physical render width (default 2400)\n"
                << "  --height N         physical render height (default 1350)\n"
                << "  --frames N         frames per run (default 240)\n"
                << "  --fps N            frame rate used for timestamps (default 60)\n"
                << "  --runs N           runs per thread level; summary reports medians (default 3)\n"
                << "  --threads A,B,C    thread levels (default 1,2,4,8,16)\n"
                << "  --font-size N      main text font size (default 80)\n"
                << "  --ruby-size N      ruby font size (default 32)\n"
                << "  --no-glow          skip glow-like offscreen pass\n"
                << "  --no-ruby          skip ruby paths\n"
                << "  --no-utopia        skip per-glyph transform\n";
            std::exit(0);
        } else {
            std::cerr << "Unknown argument: " << arg << "\n";
            std::exit(2);
        }
    }
    return opt;
}

QStringList lyricLines() {
    return {
        utf8("目移りしたい乙女心なら"),
        utf8("欲張りな夢を抱きしめて"),
        utf8("君の声が遠く揺れている"),
        utf8("瞬きの隙間で光るユートピア"),
    };
}

QStringList rubyLines() {
    return {
        utf8("めうつり"),
        utf8("よくばり"),
        utf8("きみ"),
        utf8("ゆーとぴあ"),
    };
}

double wave(double phase) {
    return std::sin(phase * 6.283185307179586);
}

QPainterPath textPath(const QString &text, const QFont &font, const QPointF &baseline) {
    QPainterPath path;
    path.addText(baseline, font, text);
    return path;
}

std::uint64_t imageChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    const qsizetype step = std::max<qsizetype>(1, size / 4096);
    for (qsizetype i = 0; i < size; i += step) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

void paintGlowPass(QPainter &painter, const QPainterPath &path, const QColor &color) {
    QPen pen(color, 18.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
    painter.strokePath(path, pen);
    QPen pen2(QColor(color.red(), color.green(), color.blue(), 95), 30.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
    painter.strokePath(path, pen2);
}

void paintStrokedText(QPainter &painter, const QPainterPath &path, const QColor &fill, const QColor &stroke) {
    painter.strokePath(path, QPen(stroke, 12.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.strokePath(path, QPen(QColor(255, 255, 255, 210), 4.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.fillPath(path, QBrush(fill));
}

std::uint64_t renderOneFrame(const Options &opt, int frameIndex) {
    QImage image(opt.width, opt.height, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    QPainter painter(&image);
    painter.setRenderHints(
        QPainter::Antialiasing |
        QPainter::TextAntialiasing |
        QPainter::SmoothPixmapTransform
    );

    const double t = static_cast<double>(frameIndex) / static_cast<double>(opt.fps);
    const auto lines = lyricLines();
    const auto rubies = rubyLines();
    QFont mainFont(utf8("Yu Gothic UI"), opt.fontSize, QFont::Black);
    QFont rubyFont(utf8("Yu Gothic UI"), opt.rubySize, QFont::DemiBold);

    const double centerX = opt.width * 0.5;
    const double startY = opt.height * 0.42;
    const double lineGap = opt.fontSize * 1.65;

    for (int line = 0; line < lines.size(); ++line) {
        const QString text = lines[line];
        const int charCount = std::max(1, static_cast<int>(text.size()));
        const double y = startY + line * lineGap;
        const double totalWidth = charCount * opt.fontSize * 0.92;
        const double baseX = centerX - totalWidth * 0.5;

        QPainterPath lineAfterPath;
        QPainterPath lineBeforePath;

        for (int i = 0; i < text.size(); ++i) {
            const QString ch = text.mid(i, 1);
            const double progress = std::fmod(t * 0.85 + i * 0.071 + line * 0.13, 1.0);
            const double bounce = opt.utopia ? wave(progress) : 0.0;
            const double scale = opt.utopia ? (1.0 + 0.13 * std::max(0.0, bounce)) : 1.0;
            const double dx = opt.utopia ? 16.0 * wave(progress + 0.31) : 0.0;
            const double dy = opt.utopia ? -24.0 * std::max(0.0, wave(progress + 0.16)) : 0.0;
            const QPointF baseline(baseX + i * opt.fontSize * 0.92 + dx, y + dy);
            QPainterPath glyph = textPath(ch, mainFont, baseline);
            if (opt.utopia) {
                QTransform transform;
                transform.translate(baseline.x(), baseline.y());
                transform.scale(scale, scale);
                transform.translate(-baseline.x(), -baseline.y());
                glyph = transform.map(glyph);
            }
            if (i <= (frameIndex / 3 + line * 4) % (text.size() + 3)) {
                lineAfterPath.addPath(glyph);
            } else {
                lineBeforePath.addPath(glyph);
            }
        }

        if (opt.glow) {
            QImage glowLayer(opt.width, opt.height, QImage::Format_ARGB32_Premultiplied);
            glowLayer.fill(Qt::transparent);
            QPainter glowPainter(&glowLayer);
            glowPainter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing);
            paintGlowPass(glowPainter, lineAfterPath, QColor(0, 130, 255, 115));
            glowPainter.end();
            painter.drawImage(0, 0, glowLayer);
        }

        paintStrokedText(painter, lineBeforePath, QColor(235, 235, 240), QColor(20, 24, 34));

        QLinearGradient afterGradient(QPointF(baseX, y - opt.fontSize), QPointF(baseX + totalWidth, y));
        afterGradient.setColorAt(0.0, QColor(255, 247, 125));
        afterGradient.setColorAt(0.55, QColor(255, 92, 184));
        afterGradient.setColorAt(1.0, QColor(70, 210, 255));
        painter.strokePath(lineAfterPath, QPen(QColor(25, 28, 36), 12.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        painter.strokePath(lineAfterPath, QPen(QColor(255, 255, 255, 230), 4.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        painter.fillPath(lineAfterPath, QBrush(afterGradient));

        if (opt.ruby) {
            const QString ruby = rubies[line % rubies.size()];
            const QPointF rubyBase(baseX + totalWidth * 0.15, y - opt.fontSize * 1.08);
            QPainterPath rubyPath = textPath(ruby, rubyFont, rubyBase);
            painter.strokePath(rubyPath, QPen(QColor(15, 18, 28), 5.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
            painter.fillPath(rubyPath, QBrush(QColor(255, 255, 255)));
        }
    }

    painter.end();
    return imageChecksum(image);
}

RenderStats runLevel(const Options &opt, int threads, int run) {
    std::atomic<int> nextFrame{0};
    std::vector<std::uint64_t> checksums(static_cast<std::size_t>(threads), 0);

    const double cpuStart = processCpuSeconds();
    CpuSampler cpuSampler;
    cpuSampler.start();
    QElapsedTimer timer;
    timer.start();

    std::vector<std::thread> workers;
    workers.reserve(static_cast<std::size_t>(threads));
    for (int tid = 0; tid < threads; ++tid) {
        workers.emplace_back([&, tid]() {
            std::uint64_t local = 0;
            for (;;) {
                const int frame = nextFrame.fetch_add(1, std::memory_order_relaxed);
                if (frame >= opt.frames) {
                    break;
                }
                local ^= renderOneFrame(opt, frame + tid * 7);
            }
            checksums[static_cast<std::size_t>(tid)] = local;
        });
    }
    for (auto &worker : workers) {
        worker.join();
    }
    const double cpuEnd = processCpuSeconds();
    cpuSampler.stop();

    const double wall = static_cast<double>(timer.nsecsElapsed()) / 1'000'000'000.0;
    const double fps = static_cast<double>(opt.frames) / wall;
    std::uint64_t checksum = 0;
    for (const auto value : checksums) {
        checksum ^= value;
    }

    RenderStats stats;
    stats.threads = threads;
    stats.run = run;
    stats.wallSeconds = wall;
    stats.fps = fps;
    stats.msPerFrame = wall * 1000.0 / static_cast<double>(opt.frames);
    stats.cpuCoresAvg = (std::isfinite(cpuStart) && std::isfinite(cpuEnd) && wall > 0.0)
        ? (cpuEnd - cpuStart) / wall
        : cpuSampler.averageCores();
    stats.cpuCoresMax = cpuSampler.maxCores();
    stats.cpuAllCorePctAvg = std::isfinite(stats.cpuCoresAvg)
        ? stats.cpuCoresAvg * 100.0 / static_cast<double>(logicalProcessorCount())
        : cpuSampler.averageAllCorePct();
    stats.cpuAllCorePctMax = cpuSampler.maxAllCorePct();
    stats.checksum = checksum;
    return stats;
}

std::vector<SummaryStats> summarizeStats(
    const std::vector<std::vector<RenderStats>> &allStats,
    double serialMedianWall
) {
    std::vector<SummaryStats> summaries;
    summaries.reserve(allStats.size());
    for (const auto &levelStats : allStats) {
        if (levelStats.empty()) {
            continue;
        }
        std::vector<double> walls;
        std::vector<double> fpsValues;
        std::vector<double> msValues;
        std::vector<double> cpuCoreAvgValues;
        std::vector<double> cpuCoreMaxValues;
        std::vector<double> cpuPctAvgValues;
        std::vector<double> cpuPctMaxValues;
        walls.reserve(levelStats.size());
        fpsValues.reserve(levelStats.size());
        msValues.reserve(levelStats.size());
        cpuCoreAvgValues.reserve(levelStats.size());
        cpuCoreMaxValues.reserve(levelStats.size());
        cpuPctAvgValues.reserve(levelStats.size());
        cpuPctMaxValues.reserve(levelStats.size());

        for (const auto &stats : levelStats) {
            walls.push_back(stats.wallSeconds);
            fpsValues.push_back(stats.fps);
            msValues.push_back(stats.msPerFrame);
            cpuCoreAvgValues.push_back(stats.cpuCoresAvg);
            cpuCoreMaxValues.push_back(stats.cpuCoresMax);
            cpuPctAvgValues.push_back(stats.cpuAllCorePctAvg);
            cpuPctMaxValues.push_back(stats.cpuAllCorePctMax);
        }

        SummaryStats summary;
        summary.threads = levelStats.front().threads;
        summary.runs = static_cast<int>(levelStats.size());
        summary.wallSecondsMedian = median(walls);
        summary.fpsMedian = median(fpsValues);
        summary.msPerFrameMedian = median(msValues);
        summary.speedupMedian = (serialMedianWall > 0.0 && summary.wallSecondsMedian > 0.0)
            ? serialMedianWall / summary.wallSecondsMedian
            : 1.0;
        summary.cpuCoresAvgMedian = median(cpuCoreAvgValues);
        summary.cpuAllCorePctAvgMedian = median(cpuPctAvgValues);
        summary.cpuCoresMaxObserved = maxFinite(cpuCoreMaxValues);
        summary.cpuAllCorePctMaxObserved = maxFinite(cpuPctMaxValues);
        summaries.push_back(summary);
    }
    return summaries;
}

void printStats(
    const std::vector<std::vector<RenderStats>> &allStats,
    const std::vector<SummaryStats> &summaries
) {
    std::cout << "\nper-run samples (not confidence intervals)\n";
    std::cout << "threads,run,wall_s,fps,ms_per_frame,cpu_cores_avg,cpu_cores_max,cpu_all_core_pct_avg,cpu_all_core_pct_max,checksum\n";
    for (const auto &levelStats : allStats) {
        for (const auto &s : levelStats) {
            std::cout << s.threads << ','
                      << s.run << ','
                      << metric(s.wallSeconds, 3) << ','
                      << metric(s.fps, 1) << ','
                      << metric(s.msPerFrame, 1) << ','
                      << metric(s.cpuCoresAvg, 1) << ','
                      << metric(s.cpuCoresMax, 1) << ','
                      << metric(s.cpuAllCorePctAvg, 1) << ','
                      << metric(s.cpuAllCorePctMax, 1) << ','
                      << s.checksum << '\n';
        }
    }

    std::cout << "\nmedian summary\n";
    std::cout << "threads,runs,median_wall_s,median_fps,median_ms_per_frame,median_speedup,cpu_cores_avg_median,cpu_all_core_pct_avg_median,cpu_cores_max_observed,cpu_all_core_pct_max_observed\n";
    for (const auto &s : summaries) {
        std::cout << s.threads << ','
                  << s.runs << ','
                  << metric(s.wallSecondsMedian, 3) << ','
                  << metric(s.fpsMedian, 1) << ','
                  << metric(s.msPerFrameMedian, 1) << ','
                  << metric(s.speedupMedian, 1) << ','
                  << metric(s.cpuCoresAvgMedian, 1) << ','
                  << metric(s.cpuAllCorePctAvgMedian, 1) << ','
                  << metric(s.cpuCoresMaxObserved, 1) << ','
                  << metric(s.cpuAllCorePctMaxObserved, 1) << '\n';
    }

    std::cout << "\n"
              << std::setw(8) << "threads"
              << std::setw(8) << "runs"
              << std::setw(12) << "med fps"
              << std::setw(12) << "med ms"
              << std::setw(10) << "speedup"
              << std::setw(12) << "cpu core"
              << std::setw(10) << "cpu %"
              << "\n";
    for (const auto &s : summaries) {
        std::cout << std::setw(8) << s.threads
                  << std::setw(8) << s.runs
                  << std::setw(12) << metric(s.fpsMedian, 1)
                  << std::setw(12) << metric(s.msPerFrameMedian, 1)
                  << std::setw(9) << metric(s.speedupMedian, 1) << "x"
                  << std::setw(12) << metric(s.cpuCoresAvgMedian, 1)
                  << std::setw(10) << metric(s.cpuAllCorePctAvgMedian, 1)
                  << "\n";
    }
}

}  // namespace

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
    QGuiApplication app(argc, argv);
    const Options opt = parseOptions(argc, argv);

    std::cout << "krok_qpainter_parallel_probe\n"
              << "Qt       : " << qVersion() << "\n"
              << "render   : " << opt.width << "x" << opt.height
              << " frames=" << opt.frames
              << " fps=" << opt.fps
              << " font=" << opt.fontSize
              << " ruby=" << (opt.ruby ? "on" : "off")
              << " glow=" << (opt.glow ? "on" : "off")
              << " utopia=" << (opt.utopia ? "on" : "off")
              << "\nruns    : " << opt.runs << " per thread level; summary reports medians"
              << "\ncpu     : process CPU time sampled every ~100ms; "
              << logicalProcessorCount() << " logical processors"
              << "\nthreads : ";
    for (std::size_t i = 0; i < opt.threadLevels.size(); ++i) {
        std::cout << (i ? "," : "") << opt.threadLevels[i];
    }
    std::cout << "\n";

    for (int i = 0; i < std::min(12, opt.frames); ++i) {
        (void)renderOneFrame(opt, i);
    }

    std::vector<std::vector<RenderStats>> allStats;
    allStats.reserve(opt.threadLevels.size());
    for (const int level : opt.threadLevels) {
        std::vector<RenderStats> levelStats;
        levelStats.reserve(static_cast<std::size_t>(opt.runs));
        for (int run = 1; run <= opt.runs; ++run) {
            levelStats.push_back(runLevel(opt, level, run));
        }
        allStats.push_back(std::move(levelStats));
    }

    double serialMedianWall = 0.0;
    for (const auto &levelStats : allStats) {
        if (!levelStats.empty() && levelStats.front().threads == 1) {
            std::vector<double> walls;
            walls.reserve(levelStats.size());
            for (const auto &stats : levelStats) {
                walls.push_back(stats.wallSeconds);
            }
            serialMedianWall = median(walls);
            break;
        }
    }
    if (serialMedianWall <= 0.0 && !allStats.empty()) {
        std::vector<double> walls;
        for (const auto &stats : allStats.front()) {
            walls.push_back(stats.wallSeconds);
        }
        serialMedianWall = median(walls);
    }

    const auto summaries = summarizeStats(allStats, serialMedianWall);
    printStats(allStats, summaries);
    return 0;
}
