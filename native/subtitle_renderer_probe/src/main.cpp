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
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Options {
    int width = 2400;
    int height = 1350;
    int frames = 240;
    int fps = 60;
    int fontSize = 80;
    int rubySize = 32;
    bool glow = true;
    bool ruby = true;
    bool utopia = true;
    std::vector<int> threadLevels{1, 2, 4, 8};
};

struct RenderStats {
    int threads = 1;
    double wallSeconds = 0.0;
    double fps = 0.0;
    double msPerFrame = 0.0;
    double speedup = 1.0;
    std::uint64_t checksum = 0;
};

QString utf8(const char *text) {
    return QString::fromUtf8(text);
}

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
                << "  --threads A,B,C    thread levels (default 1,2,4,8)\n"
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

RenderStats runLevel(const Options &opt, int threads, double serialWall) {
    std::atomic<int> nextFrame{0};
    std::vector<std::uint64_t> checksums(static_cast<std::size_t>(threads), 0);

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

    const double wall = static_cast<double>(timer.nsecsElapsed()) / 1'000'000'000.0;
    const double fps = static_cast<double>(opt.frames) / wall;
    std::uint64_t checksum = 0;
    for (const auto value : checksums) {
        checksum ^= value;
    }

    RenderStats stats;
    stats.threads = threads;
    stats.wallSeconds = wall;
    stats.fps = fps;
    stats.msPerFrame = wall * 1000.0 / static_cast<double>(opt.frames);
    stats.speedup = serialWall > 0.0 ? serialWall / wall : 1.0;
    stats.checksum = checksum;
    return stats;
}

void printStats(const std::vector<RenderStats> &stats) {
    std::cout << std::fixed << std::setprecision(2);
    std::cout << "\nthreads,wall_s,fps,ms_per_frame,speedup,checksum\n";
    for (const auto &s : stats) {
        std::cout << s.threads << ','
                  << s.wallSeconds << ','
                  << s.fps << ','
                  << s.msPerFrame << ','
                  << s.speedup << ','
                  << s.checksum << '\n';
    }

    std::cout << "\n"
              << std::setw(8) << "threads"
              << std::setw(10) << "wall(s)"
              << std::setw(10) << "fps"
              << std::setw(12) << "ms/frame"
              << std::setw(10) << "speedup"
              << "\n";
    for (const auto &s : stats) {
        std::cout << std::setw(8) << s.threads
                  << std::setw(10) << s.wallSeconds
                  << std::setw(10) << s.fps
                  << std::setw(12) << s.msPerFrame
                  << std::setw(9) << s.speedup << "x"
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
              << "\nthreads : ";
    for (std::size_t i = 0; i < opt.threadLevels.size(); ++i) {
        std::cout << (i ? "," : "") << opt.threadLevels[i];
    }
    std::cout << "\n";

    for (int i = 0; i < std::min(12, opt.frames); ++i) {
        (void)renderOneFrame(opt, i);
    }

    std::vector<RenderStats> stats;
    stats.reserve(opt.threadLevels.size());
    double serialWall = 0.0;
    for (const int level : opt.threadLevels) {
        RenderStats s = runLevel(opt, level, serialWall);
        if (level == 1 || serialWall <= 0.0) {
            serialWall = s.wallSeconds;
            s.speedup = 1.0;
        }
        stats.push_back(s);
    }
    printStats(stats);
    return 0;
}
