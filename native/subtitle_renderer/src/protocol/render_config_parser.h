#pragma once

#include "render_config.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

ResolvedStyle resolvedRubyStyle(const ResolvedStyle &base, const QJsonObject &overrides);

std::optional<RenderConfig> parseRenderConfig(
    const QJsonObject &ir,
    QString *error
);
// 差分重配（gpu_configure_style）：在既有 config 携带的行数据（lines /
// rubies / 矢量符号表 / 主轨偏移）上重放 patch 里的 screen/style/
// fx_sprites/titles/lines_style 段。样式派生状态在全新默认构造的 config
// 上重放，「键缺席 → 默认值」与全量解析完全一致；行级样式派生字段
// （动画/信号旗标/粒子 bursts）按 (source_index, source_line_index) 差分
// 合并。画面段漂移或行集对不上返回 nullopt 并写 error（调用方回落全量
// configure）。
std::optional<RenderConfig> applyRenderConfigStylePatch(
    const QJsonObject &patch,
    const RenderConfig &current,
    QString *error
);
ResolvedStyle resolvedStyleFromTitle(
    const ResolvedStyle &base,
    const QJsonObject &title
);
QString resolvedStyleKey(int singerId, const QString &roleLabel);
const ResolvedStyle &resolvedStyleForLine(
    const RenderConfig &cfg,
    const TimingLine &line
);
const ResolvedStyle &resolvedStyleForCharacter(
    const RenderConfig &cfg,
    const TimingLine &line,
    const TimingChar &ch
);
// ``roleLabel`` 的方案叠加到 ``lineStyle`` 上（无方案时原样返回）；供
// 指示灯/音量柱 ``role`` 外观档在场景投影里解析固定装饰源。
ResolvedStyle resolvedStyleForRole(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const QString &roleLabel
);
// 标题导唱符投影复用歌词字符的解析器：位图读 bitmap_guide IR 键名，
// 矢量读内嵌 path_commands 轮廓（render_ir._title_guide_to_ir 同构）。
std::optional<krok::subtitle::native::VectorGlyph> parseVectorGlyph(
    const QJsonValue &value
);
std::optional<krok::subtitle::native::BitmapGuide> parseBitmapGuide(
    const QJsonValue &value
);

}  // namespace krok::subtitle::native::protocol

