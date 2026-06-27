# SUG 与字幕渲染模块 Python 走字逻辑差异

> 本文比较歌词打轴子模块 StrangeUtaGame（下称 **SUG**）的 `KaraokePreview`，与工作台字幕渲染模块（下称 **字幕模块**）当前 **Python QPainter** 渲染路径。字幕模块暂缓中的 C++/native 渲染核心不在比较范围内。

## 1. 对比基线与代码范围

### 1.1 SUG

- 子模块提交：`ed3758b8786b4263d803f0ed1775705153d247ac`（`SUGv1.2.6`）
- 预览核心：[`karaoke_preview.py`](../krok_helper/lyrics_timing/src/strange_uta_game/frontend/editor/timing/karaoke_preview.py)
- 数据模型：[`models.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/domain/models.py)
- Nicokara 导出：[`nicokara_exporter.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/infrastructure/exporters/nicokara_exporter.py)
- 完整梳理见：[歌词打轴子模块 `karaoke_preview` 走字逻辑](歌词打轴子模块-karaoke_preview走字逻辑.md)

### 1.2 字幕模块

- 主仓库提交：`c77f74e9ade13ec4e96e40f0cef4eb1777340238`
- 开发分支：`feat/subtitle-render`
- LRC 解析：[`subtitle_sources.py`](../krok_helper/subtitle_render/subtitle_sources.py)
- 时间区间：[`timeline.py`](../krok_helper/subtitle_render/engine/timeline.py)
- Python 绘制：[`painter.py`](../krok_helper/subtitle_render/engine/painter.py)
- 数据模型：[`models.py`](../krok_helper/subtitle_render/models.py)
- 预览时钟：[`preview_view.py`](../krok_helper/subtitle_render/frontend/preview_view.py)
- 导出逐帧时钟：[`renderer.py`](../krok_helper/subtitle_render/engine/renderer.py)

### 1.3 对齐进度

- 2026-06-27：**无独立时间戳的多字分配已对齐**。解析器保留
  ``[start]多字[next]`` 共享时间块，横排 Python Painter 按当前主文字字符布局宽度
  分配区间，不再以 codepoint 数量等分最终走字。
- 2026-06-27：**可由 `@Ruby` 恢复的多 checkpoint 主文字已对齐**。主文字使用
  ``[pos_start, reading_part_ms..., pos_end]`` 作为独立锚点轴，每个 checkpoint 段
  等分主文字进度；不再复用 ruby reading unit 的 mora/停顿进度。无 ruby 字符的额外
  checkpoint 未写入 Nicokara，仍属于格式层不可恢复信息。
- 2026-06-27：**ruby 空间权重已对齐**。解析器保留 `@Ruby` 内时间戳切开的原始
  `reading_parts`，Python Painter 使用当前 ruby 字体的 `horizontalAdvance` 作为各
  part 权重；空 part 消耗时间但不推进扫光。
- 2026-06-27：**行首/行尾缺锚点的跨行借时已对齐核心规则**。解析器会让行首无时间戳
  字符借上一条可用行尾，并把这段标成共享时间块；行尾缺结束点时会借下一条可唱行的
  leader。LRC 仍无法表达 SUG 的未完整打轴屏障，也没有音频总时长兜底信息。

## 2. 结论先行

两边都采用“先画未唱层，再裁切叠加已唱层”的基本模型，主文字也都已改为按字形墨水边界推进。但它们目前**不是同一套走字算法**。

当前最影响肉眼一致性的剩余差异有两项：

1. **数据源不同**：SUG 直接读取内存中的毫秒级完整 checkpoint；字幕模块读取经过 Nicokara LRC 导出、厘秒量化和重新解析后的中间数据。
2. **无 ruby 多 checkpoint 仍不可恢复**：已有 `@Ruby` 的主文字已使用全部 checkpoint；但 SUG 即使没有 ruby 也能使用第二、第三 checkpoint，而字幕 LRC 正文只保留每字第一个 checkpoint。

无独立时间戳的普通多字块现已按主文字布局宽度分时；但 SUG 的布局宽度还可能被
ruby 和编辑器 checkpoint marker 撑大，字幕模块没有 marker、也不让 ruby 反向撑开
主文字，所以涉及这些编辑器布局因素时仍不是逐毫秒完全等价。

## 3. 共通点

先列出已经对齐的部分，避免把所有视觉差异都误判为时间算法问题。

### 3.1 都是 before/after 双层模型

两边都遵循：

1. 画完整未唱状态；
2. 根据当前进度生成裁切区域；
3. 在裁切区域内画已唱状态；
4. 完全唱完后直接画完整已唱状态。

因此两边都不是“逐字瞬间换色”，而是字内连续扫光。

### 3.2 横排主文字都按墨水边界推进

- SUG 使用 `QFontMetrics.tightBoundingRect()`；
- 字幕模块使用与实际绘制同源的 `QPainterPath.boundingRect()`。

两者都避开 advance box 两侧的透明 side bearing。空白字符仍占布局或时间，但没有可见墨水。

实现口径相近，但不是逐像素完全相同：

- SUG 的边界和裁切宽度主要取整数并向下截断；
- 字幕模块常使用 `floor/ceil` 建边界、`round` 计算锋面；
- 字体 hinting、路径包围盒与 `tightBoundingRect` 也可能相差约 1 px。

### 3.3 行内停顿的基本语义相同

对 `[start]字[release][next]下一字`：

- 前一字在 `start → release` 完成；
- `release → next` 保持不推进；
- 下一字从 `next` 开始。

SUG 从 `is_sentence_end/global_sentence_end_ts` 得到此语义；字幕模块把连续时间戳解析成 `pause_release_ms`。

### 3.4 正偏移的目标语义相同

正偏移都表示字幕延后：

- SUG 把偏移加到字符时间戳上；
- 字幕模块用 `track_t = playback_t - offset` 采样更早的字幕时间。

在没有重复写入偏移、没有 0 ms 钳制影响时，两种写法数学等价。

## 4. 整体架构差异

### 4.1 SUG：实时编辑模型直接渲染

```text
Project / Character
  ├─ 全部 checkpoint（毫秒）
  ├─ check_count
  ├─ linked_to_next
  ├─ Ruby.parts
  ├─ singer_id
  └─ 未完整打轴状态
          │
          ▼
KaraokePreview._get_sentence_render_data()
          │
          ▼
paintEvent(current_audio_time)
```

优点是所有编辑语义都还在；缺点是走字时间会受预览字体、ruby 宽度、checkpoint marker 宽度等编辑器布局因素影响。

### 4.2 字幕模块：交换格式重建后渲染

```text
SUG Project
  → NicokaraExporter
  → .lrc（厘秒；正文 + @Ruby）
  → subtitle_sources.parse_nicokara_lrc()
  → TimingTrack / TimingChar / RubyAnnotation
  → compute_char_intervals()
  → painter._karaoke_fill_segments()
  → paint_frame(t_ms)
```

这一层转换会主动合成一些信息，也会永久丢掉一些信息。字幕 Painter 得到的并不是 SUG `Character` 的等价副本。

## 5. 时间精度与时钟

### 5.1 时间戳精度

| 项目 | SUG 预览 | 字幕模块 |
|---|---|---|
| 内部 checkpoint | 毫秒整数 | 从 LRC 厘秒恢复为毫秒，通常 10 ms 网格 |
| 导出量化 | 无 | SUG `_format_nicokara_ts()` 四舍五入到 10 ms |
| ruby 内部时间 | 原始毫秒 checkpoint | `@Ruby` 内相对时间同样量化到 10 ms |
| 输出帧 | Qt paint 时刻 | 导出时 `round(frame_index × 1000 / fps)` |

因此即使算法完全相同，字幕模块也可能比 SUG 预览早或晚数毫秒；最终视频还受 60/120 fps 帧边界量化。

### 5.2 预览播放时钟

SUG：

- 外部 16 ms 定时器请求刷新；
- `paintEvent()` 绘制瞬间再次主动读取 BASS `get_display_position_ms()`；
- 显示时间已扣输出延迟，并在播放中做单调不回退保护。

字幕模块：

- 用 `QElapsedTimer` 连续外推 UI 时间；
- 以 60/120 fps 的精确定时器 tick 更新；
- 周期性向 `QMediaPlayer.position()` 或共享播放器位置收敛；
- 30 ms 内不纠偏，中等偏差每 tick 纠 10%，超过 250 ms 直接吸附。

这意味着同一音频播放时，两边的“当前毫秒”也可能短暂不同。SUG 更贴近 BASS 当前显示位置；字幕预览更强调墙钟平滑并缓慢跟随媒体时钟。

### 5.3 离线导出

字幕模块的 `paint_frame(track, t_ms, style)` 是纯时间采样：相同输入和 `t_ms` 应得到确定结果。离线导出不使用预览墙钟，而按输出 fps 枚举帧时间。

SUG `KaraokePreview` 是交互预览控件，没有对应的逐帧离线输出职责。

## 6. Nicokara 中间层造成的信息变化

### 6.1 正文只导出每字第一个 checkpoint

SUG Nicokara 正文对有时间戳的字符只写：

```text
[character.global_timestamps[0]]字符
```

第二、第三 checkpoint 不进入正文。它们只有在存在 ruby 时，才可能以 `@Ruby` 读音内的相对时间戳保留下来。

后果：

- **有 ruby**：字幕模块通过 `RubyAnnotation.reading_part_ms` 恢复多段节奏，并以
  主文字专用锚点算法消费全部 checkpoint；
- **无 ruby**：多 checkpoint 信息不可恢复，主文字退化为首时间戳到下一边界的整段线性走字。

SUG 预览不存在这种退化，因为它直接读取全部 `global_timestamps`。

### 6.2 `linked_to_next` 不直接进入 `TimingTrack`

SUG 用显式布尔字段 `linked_to_next` 建连词组。

字幕模块的 `TimingChar` 没有对应字段；连词关系只能通过多字 `@RubyN=漢字,読み,...` 间接重建。因此：

- 有多字 ruby 的连词通常能恢复；
- 没有 ruby 的显式连词会丢失；
- ruby 文本匹配失败或匹配到错误重复词时，连词目标范围会变化。

### 6.3 厘秒量化

正文时间、行尾释放点、ruby `pos1/pos2` 与 ruby 内相对 checkpoint 都被量化到厘秒。多个相距不足约 5 ms 的点可能合并到同一个时间。

字幕模块会容忍零时长区间并让它在时间越过起点后瞬间完成；SUG 原始毫秒数据仍可能保留一个极短但非零的渐变。

### 6.4 当前偏移导出值得注意

当前 SUG 正文和 `@Ruby` 位置使用的是已经加过 `global_offset_ms` 的 `global_timestamps`。

但 exporter 写 `@Offset` 时读取的是 `project.offset_ms`，而当前 `Project` 模型持久字段名是 `global_offset_ms`。按标准调用路径，正文时间通常已经带偏移，而 `@Offset` 不会再输出。

字幕模块仍会对任何实际存在的 `@Offset` 再做 `playback_t - offset`。若外部文件既把偏移烘进正文、又保留 `@Offset`，会产生二次偏移风险。

## 7. 无时间戳字符的时间分配

这是普通歌词中最常见、也最容易看出差异的一项。

### 7.1 SUG：按当前布局像素宽度分配

同一 leader 与下一时间锚点之间，SUG 把 leader 及其后无首时间戳字符放在一起，以 `char_widths` 加权分配时间。

`char_widths` 不只是主文字 advance：

- 单字格会被 ruby 宽度撑大；
- 连词组总格会被合并 ruby 撑大，再平均分给组内字符；
- checkpoint marker 过宽也会撑大字符格。

所以 SUG 的时间边界可能随主字体、ruby 字体、ruby 内容或 marker 设置变化。

### 7.2 字幕模块：已改为渲染时按字符布局宽度分配

解析器仍会生成按 codepoint 等分的兼容 `start_ms`，供没有字体度量的时间轴消费者使用；
同时在每个 `TimingChar` 上保留：

```text
source_span_start_ms
source_span_end_ms
source_span_index
source_span_count
```

横排 Python Painter 完成字体布局、得到 `char_widths` 后，再调用：

```text
compute_char_intervals(line, char_widths)
```

共享块的最终字符边界使用与 SUG 相同的累计宽度公式：

```text
boundary = int(start + duration × cumulative_width / total_width)
```

普通行和行内角色多字体行都走这条路径；Utopia 等逐字动画也复用 layout 产生的加权区间。

### 7.3 对齐后的结果

设 `[1000]AB[2000]`，且 A 的布局宽度是 B 的 3 倍：

- SUG：A 约占 750 ms，B 约占 250 ms；
- 字幕模块横排 Python Painter：A 约占 750 ms，B 约占 250 ms。

无字体度量的消费者仍能看到解析阶段的 500/500 兼容区间，但实际 Python 画面使用
750/250。当前 native/C++ 核心不在本次改动范围内。

### 7.4 当前对齐边界

- 已对齐：主文字字体改变 advance 时，两边都会重新按当前字符布局宽度分时；
- 仍不同：SUG 的 `char_widths` 还可能被 ruby 宽度和 checkpoint marker 撑大；
- 字幕模块没有编辑器 marker，且当前 ruby 不反向修改主文字 advance；
- 空白或异常零总宽块会安全回退到解析阶段的兼容区间。

本次对齐锁定的是“无独立时间戳多字按主文字渲染宽度分配”这一核心规则，没有复制
SUG 的编辑器专属 marker 布局副作用。

## 8. 行首、行尾与跨行锚点

### 8.1 行首无首时间戳字符

SUG：

- 尝试向前跨行借最近有效末时间；
- 从借到的时间走到本行首 leader；
- 中间可跨过全 `cc=0` 且无时间戳的空行；
- 遇到未完整打轴行则停止，不走字。

字幕模块：

- 把第一个 LRC 时间戳之前缓存的可见字符全部补回；
- 如果上一条可唱行有可用行尾，这些字符会借上一行尾到本行首 leader 的区间；
- 该区间会标为共享时间块，解析阶段兼容等分 `start_ms`，横排 Python Painter 再按
  字形布局宽度重分配；
- 如果找不到上一条可用行尾，则仍统一使用第一个时间戳作为兼容起点。

### 8.2 行尾缺少结束点

SUG：

1. 尝试借后续最近已打轴行的首时间；
2. 找不到时使用音频总时长；
3. 遇到未完整打轴屏障则不借。

字幕模块：

- `TimingLine.end_ms is None` 且后续存在可唱行时，借后续最近可唱行的 leader；
- 后续无可唱行时，仍保留 `None`，timeline/painter 使用既有安全兜底；
- LRC 没有音频总时长字段，所以无法在解析阶段复刻 SUG 的“借音频总时长”分支。

因此“借下一行首”这一核心规则已对齐；末行无结束点、且只有音频总时长可借的场景仍不同。

### 8.3 未完整打轴屏障

SUG 能识别：

- `check_count > 0` 但没有时间戳；
- 标记了演唱停顿但没有释放时间戳。

它们会阻断跨行借锚点。

Nicokara LRC 不保留完整的 `check_count` 与“应该有但尚未打”的状态。字幕模块只能看到已有文本/时间戳，因此没有等价屏障，可能会按相邻行首/行尾继续借时。

## 9. 主文字多 checkpoint

### 9.1 SUG

普通多 checkpoint 字符会建立：

```text
[ts0, ts1, ..., tsN, character_window_end]
```

主文字总墨水按时间段数等份推进。特殊的“连词内多 cp leader + 后随 `cc=0` 字符”会把整组主文字墨水合成一条轴，并在条件满足时按 ruby part 宽度加权。

### 9.2 字幕模块

没有 ruby 的字符只有 `(start_ms, end_ms)`，整字线性；Nicokara 正文没有携带额外
checkpoint，Painter 无从恢复。

有 `RubyAnnotation` 时，`_karaoke_fill_segments()` 会把 ruby 命中的主文字索引合为
segment，并使用 `_main_text_ruby_progress_ratio()` 驱动主文字锋面。其锚点为：

```text
[ruby.pos_start_ms,
 ruby.pos_start_ms + reading_part_ms[0],
 ruby.pos_start_ms + reading_part_ms[1],
 ...,
 ruby.pos_end_ms]
```

各相邻 checkpoint 时间段等分主文字总进度，与 SUG 普通
`char_part_anchors → _anchor_ratio()` 一致。重复/越界相对时间会按有效区间单调钳制。

Ruby 自身仍走独立的 `_ruby_progress_ratio()`：它按原始 `reading_parts` 的实际像素宽度
累计；主文字则按 checkpoint 段等分。两套 ratio 有意分离，因此 part 宽度只改变 ruby
锋面，不会误改主文字的多 checkpoint 节奏。

因此当前范围是：**能被 `@Ruby` 映射覆盖的主文字支持全部可恢复 checkpoint；无 ruby
多 checkpoint 仍需额外 IR/WorkflowContext 才能无损支持。**

### 9.3 分组条件不同

SUG 特殊合并主文字轴要求：

- 明确处于 `linked_to_next` 组；
- leader 至少两个时间戳；
- 后随字符全部 `cc=0`；
- 时间戳严格递增；
- 有有效组尾终点。

字幕模块只要某个 `RubyAnnotation` 能通过 `kanji` 文本或时间重叠命中多个字符，就会把该范围作为 ruby segment；不检查 SUG 的上述 `cc=0` 条件。

## 10. 连词与主文字扫光范围

### 10.1 SUG

- `linked_to_next` 是连词唯一权威字段；
- 连词本身通常只改变 ruby 合并和连词框；
- 只有特殊多 checkpoint 场景才把主文字墨水合成组轴；
- 特殊组轴的总长度是各字 tight ink width 之和，字符间透明留白不占进度。

### 10.2 字幕模块

- 通过 `@Ruby.kanji` 在当前行文本中定位字符范围；
- 有重复词时优先选择与 `pos_start/pos_end` 时间重叠最多、距离最近的 occurrence；
- 多字 ruby 对应的主文字 segment 从组内最左墨水坐标到最右墨水坐标；
- 这段几何宽度包含字符间距和字形之间的透明空隙。

所以多字 ruby 主文字的扫光空间轴也不同：

- SUG 特殊组：拼接“纯墨水宽度”；
- 字幕模块 ruby 组：使用“最左到最右的整体几何跨度”。

## 11. Ruby 时间轴差异

### 11.1 SUG 单字符 ruby

- 使用父字符全部 checkpoint 加窗口终点；
- 若 `ruby.parts` 数与时间段数匹配，各段对总横向进度的贡献按 part `horizontalAdvance` 加权；
- 不匹配时才按段数等分。

### 11.2 SUG 连词 ruby

- 摊平组内各成员的实际 checkpoint 与 ruby part；
- 无自身 checkpoint 的成员可用其主文字窗口起点；
- 最后一段延伸到组尾真实结束时间；
- part 像素宽度决定累计进度；
- 时间空隙保持进度不动。

### 11.3 字幕模块 ruby（已对齐空间权重）

LRC 解析器现在同时保留：

- 去掉内嵌时间戳后的 `reading`；
- 相对 `pos_start_ms` 的 `reading_part_ms`；
- 被每个时间戳切开的原始 `reading_parts`，包括连续时间戳之间的空 part。

正常的 Nicokara `@Ruby` 数据满足
`len(reading_parts) == len(reading_part_ms) + 1`。`_ruby_progress_ratio()` 按原始
checkpoint 区间推进，并使用当前 ruby 字体度量得到：

```text
(已完成 part 像素宽度 + 当前 part 像素宽度 × 局部比例) / 总像素宽度
```

这与 SUG 单字符 ruby 的 `horizontalAdvance(part.text)` 权重规则一致。连续时间戳产生的
空 part 权重为 0，因此会消耗该段时间、但锋面保持不动。仅当输入没有可用的原始 part
边界或 part 总宽度为 0 时，才回退到 reading unit 等份。

### 11.4 直接结果

若两个 ruby part 视觉宽度为 3:1：

- SUG：前 part 通常推动约 75% 的 ruby 墨水；
- 字幕模块：前 part 同样推动约 75% 的 ruby 扫光宽度。

普通横排 ruby 在相同字体度量下的空间进度已对齐。剩余差异主要来自两边 ruby 布局
几何、连词范围重建和 LRC 厘秒量化，而不是 part 权重公式。

### 11.5 暂停表达也不同

SUG group ruby 通过分段累计宽度自然表达“某段消耗时间但不推进墨水”。

字幕模块现在直接从连续时间戳间保留下来的空 `reading_part` 表达同一行为。异常、手工
构造且没有 `reading_parts` 的旧数据仍走 reading unit 回退，因此这部分脏数据行为不保证
与 SUG 相同。

## 12. Ruby 与主文字的范围映射

### 12.1 SUG：对象关系直接可靠

Ruby 挂在具体 `Character` 上；连词由相邻 `linked_to_next` 连接。没有文本搜索歧义。

### 12.2 字幕模块：时间重叠 + 文本查找

`RubyAnnotation` 是行外列表，映射流程为：

1. 先找与 `pos_start/pos_end` 重叠的字符区间；
2. 若有 `kanji`，在整行拼接文本中查找相同子串；
3. 多个 occurrence 时用时间重叠和距离评分选一个；
4. 找到后以字符区间重新生成 effective ruby 起止时间。

潜在差异：

- 同一词在一行出现多次时可能选到不同 occurrence；
- `kanji` 与实际正文不匹配时，ruby 不参与主文字分组；
- `TimingChar.text` 若含多个 codepoint，会按其内部子串比例切目标 x 范围；
- 全局位置 `(0, 0)` 的 ruby 会在每行按文本重新匹配。

SUG 没有这些匹配层。

## 13. Ruby 布局差异

### 13.1 SUG 会让 ruby 反向影响主文字布局

- 单字字符格至少与 ruby 一样宽；
- 连词组总字符格至少与合并 ruby 一样宽；
- 扩大的宽度平均分给连词组成员；
- 主文字在各自扩大的格内居中；
- 合并 ruby 在整组中居中；
- 连词 ruby 外绘制半透明框。

### 13.2 字幕模块不让 ruby 撑开主文字

- 主文字 advance 和字间距先独立排好；
- ruby 的目标区间来自主文字现有字符范围；
- ruby 比目标窄时：通常居中；目标比自然 ruby 宽 15% 以上且有多个 unit 时，会把 units 均匀摊到目标槽位；
- ruby 比目标宽时：保持自然宽度并从目标左缘开始，可能向右超出；
- 不绘制 SUG 式连词框。

因此加载同一首歌时，连词主文字的 x 坐标、整行总宽、ruby 起点和行居中位置都可能不同，哪怕时间算法暂时一致。

## 14. 颜色与裁切层差异

### 14.1 SUG

- 未唱层按“过去行 / 当前行 / 未来行”使用主题色；
- 已唱层按每个 `Character.singer_id` 取演唱者色；
- split 演唱者把字形墨水高度切成 2～5 条水平色带；
- 连词 ruby 全组取组首字符颜色；
- 只处理文字填充，不绘制字幕级双描边、发光或投影；
- 可叠加打轴预览指引 alpha；
- 有当前字符底框、下划线、checkpoint marker、连词框等编辑器元素。

### 14.2 字幕模块

- 使用完整 before/after 配色矩阵；
- 每个状态都可分别配置 text、stroke、stroke2、shadow/glow；
- 每层 PaintFill 支持纯色、渐变、上下分色、图片填充；
- 可按行 singer scheme 覆盖，也可用行内角色标签切换字体和整套样式；
- ruby 可有独立 before/after 配色矩阵；
- 没有 SUG 的编辑光标、checkpoint、走字指引和连词框。

### 14.3 字幕模块的锋面不只裁填充

字幕模块会让已唱状态的填充、描边、二重描边、阴影/发光一起过渡。发光需要比字形更宽的软裁切，但扫光前缘仍尽量锁在锋面，避免已唱光晕提前染到未唱笔画。

SUG 只裁文字填充，不存在描边与光晕边缘的额外视觉扩散。

## 15. 行状态与显示窗口

SUG 是编辑器多行列表：

- 当前播放行可放大；
- 过去/未来行仍在视口内；
- 自动滚动与编辑光标可分离；
- 行切换只影响字号、底色和视口，不改变字符 ratio。

字幕模块是成片布局：

- 默认双行 lane；
- 有 lead-in、tail、同 lane 冲突保护、分段、同步退场；
- 某行可以在开唱前显示，未唱状态固定由样式决定；
- 支持入场/退场动画。

所以比较截图时必须先区分“哪几行正在显示”和“行内锋面在哪”。前者本来就不应与 SUG 编辑器视口完全相同。

## 16. 方向与动画能力

### 16.1 SUG

- 当前走字只实现横排左到右；
- 支持整行左/中/右对齐；
- 没有逐字入退场动画；
- ruby 始终在主文字上方。

### 16.2 字幕模块

- 横排 LTR：左到右；
- 横排 RTL：字符排布、主文字锋面和 ruby 读音顺序均反转；
- 竖排：字符上到下、锋面上到下，ruby 在右侧；
- 支持 fade、slide、rise、char_fade、spin_flip、utopia 等动画；
- Utopia 在字内走字期间会做放大回弹，多字 ruby 还能把主文字作为组处理；
- Utopia 退场阶段会强制已唱比例为 1，避免旋转后的字形被水平 clip 再次裁掉。

这些是字幕模块的成片能力，不应为了“完全复刻 SUG 编辑器”而删除；对比纯走字时应先把方向设为 LTR、关闭所有动画。

## 17. 缓存与绘制方式

### 17.1 SUG

- 缓存每行的布局宽度、时间窗口、连词/ruby 分段数据；
- 每帧仍由 `QPainter.drawText()` / clip 直接绘制；
- 跨行借锚点导致修改一行时要双向扩散失效缓存。

### 17.2 字幕模块

- layout 与当前 `t_ms` 分离；
- 默认把 before/after 主文字和 ruby 层烘焙成位图缓存；
- 每帧主要做 blit + 动态 clip；
- 保留 direct vector oracle，可与 layer 路径做像素回归；
- 离线导出与预览复用同一个 `paint_frame` 语义。

字幕模块的缓存架构更适合逐帧视频生产，但这不自动保证时间语义与 SUG 一致。

## 18. 典型差异场景

### 18.1 多字共享两个时间戳

```text
[1000]Wide窄[2000]
```

- SUG：按实际布局宽度瓜分 1000 ms；
- 字幕模块横排 Python Painter：同样按当前主文字布局宽度瓜分 1000 ms；
- 解析器保留的 codepoint 等分时间只作为无字体消费者的兼容值，不再决定 Python 画面。

这一项已经对齐；若仍看到差异，应继续检查 ruby/marker 是否撑宽了 SUG 字符格。

### 18.2 无 ruby 的多 checkpoint 字

```text
字 X checkpoints = [1000, 1250, 1800]
窗口终点 = 2400
ruby = None
```

- SUG：主文字分三段推进；
- 字幕 LRC：正文只剩 `[1000]X`，1250/1800 不可恢复；
- 字幕模块：X 在 1000～2400 整段线性。

这是信息损失，单改 Painter 无法补回。

若 X 有 `@Ruby`，1250/1800 会进入 `reading_part_ms`，字幕模块现已用
`[1000, 1250, 1800, 2400]` 驱动主文字，与 SUG 普通多 checkpoint 主文字一致。

### 18.3 单字两段 ruby 宽度不等

```text
ruby.parts = [宽 part, 窄 part]
```

- SUG：横向进度按两个 part 的像素宽度分配；
- 字幕模块：同样按两个原始 part 的 `horizontalAdvance` 分配。

该场景的空间权重已对齐；字体、布局范围不同时仍可能出现像素位置差异。

### 18.4 行尾没有释放点

- SUG：尾字可一直走到下一有效行首或音频结束；
- 字幕模块：有下一条可唱行时同样走到下一行 leader；末行无结束点时仍走 fallback。

### 18.5 行首有无时间戳连读字

- SUG：可能从上一行末借时间平滑进入首 leader；
- 字幕模块：有上一条可用行尾时同样借时间进入首 leader；无可借行尾时仍同首时间戳。

### 18.6 多字熟字训 ruby

- SUG：显式 linked group，ruby 影响主文字布局；特殊条件下主文字才按合并轴推进；
- 字幕模块：通过 `@Ruby.kanji` 找到整个词，主文字通常直接由 ruby ratio 驱动，ruby 不撑开主文字。

## 19. 差异矩阵

| 维度 | SUG `KaraokePreview` | 字幕模块 Python Painter | 是否会明显影响画面 |
|---|---|---|---|
| 输入 | 内存 Project | Nicokara LRC 重建 IR | 是 |
| 精度 | 原始毫秒 | 厘秒量化 + 帧量化 | 小幅 |
| `cc=0` 多字 | 布局宽度加权 | 横排 Python 已按布局宽度加权 | **已对齐核心规则** |
| 行首无锚点 | 跨行借时间 | 上一可用行尾；无可借行尾时同首时间戳 | **核心规则已对齐** |
| 行尾无结束点 | 下一行首/音频结束 | 下一可唱行 leader；末行仍 fallback | **下一行首已对齐** |
| 未完成屏障 | 有 | 无等价信息 | 是 |
| 有 `@Ruby` 多 checkpoint | 保留 | 以全部原始锚点驱动主文字 | **已对齐普通字符规则** |
| 无 ruby 多 checkpoint | 保留 | Nicokara 正文丢失额外点 | 仍不同 |
| 有 ruby 主文字组 | 特定条件合并 | ruby 命中即合并 segment | 是 |
| ruby 空间权重 | part 像素宽度 | part 像素宽度 | **已对齐核心规则** |
| 连词来源 | `linked_to_next` | `@Ruby.kanji` 文本/时间匹配 | 是 |
| ruby 撑主文字 | 会 | 不会 | 是 |
| ruby 连词框 | 有 | 无 | 外观不同 |
| 墨水边界 | tightBoundingRect | QPainterPath bounds | 轻微 |
| 已唱视觉层 | 文字填充 | 填充+双描边+阴影/发光 | 是 |
| per-char 角色 | 直接 singer_id | LRC 标签/方案匹配 | 可能 |
| 打轴指引 | 有 | 无 | 仅编辑态 |
| RTL/竖排 | 无 | 有 | 模式差异 |
| 逐字动画 | 无 | 有 | 开启时显著 |
| 离线确定性 | 非目标 | 同一 `t_ms` 确定渲染 | 架构差异 |

## 20. 若要让字幕模块的普通横排走字更接近 SUG

建议先明确目标是“复刻编辑器当前观感”，还是“保留成片渲染更稳定的时间模型”。两者并不完全相同。

若以 SUG 观感为准，优先级建议如下。

### P0：先解决不可恢复和大幅时差

1. **建立 Project → LRC → TimingTrack 对照夹具**：覆盖每字有轴、`cc=0`、多 checkpoint、行首借轴、行尾无释放、多字 ruby。
2. **决定无 ruby 多 checkpoint 的传递方式**：有 `@Ruby` 的可恢复路径已完成；现有
   Nicokara 正文仍无法携带无 ruby 字符的全部点。需要扩展 IR/WorkflowContext 直传，
   或定义工作台私有元数据；仅改 Painter 无解。
3. ~~统一行尾无结束点策略：借下一行首。~~ **已完成（末行音频总时长兜底仍缺源数据）**。
4. ~~统一行首无时间戳字符策略：跨行借上一行尾。~~ **已完成（未完成屏障仍缺源数据）**。

### P1：统一正常连读和 ruby 速度

1. ~~把多字共享时间块延迟到布局阶段，按主文字 advance 分配。~~ **已完成（Python 横排）**；
2. 决定是否继续复刻 SUG 中 ruby/marker 对字符时间权重的额外撑宽；
3. ~~让 `_ruby_progress_ratio()` 按原始 ruby part 像素宽度累计，而非固定等份。~~ **已完成（Python Painter）**；
4. 统一多字 ruby 主文字 segment 是否包含字符间透明间距。

### P2：只处理视觉外观

1. 决定长 ruby 应撑开主文字、居中溢出，还是保持当前从左缘延伸；
2. 如需编辑器观感，可在预览调试模式绘制连词框，但不建议进入最终视频；
3. 对齐 1 px 级墨水边界取整；
4. 比较前先关闭动画、RTL、竖排、描边差异和不同配色，隔离纯时间锋面。

## 21. 不建议直接照搬的 SUG 行为

以下行为服务于打轴编辑器，不一定适合作为最终字幕规范：

- checkpoint marker 宽度参与 `cc=0` 时间分配；
- 修改 ruby 字号可能改变主文字逐字时间分界；
- 过去/当前/未来行主题底色；
- 打轴预览指引 alpha；
- 编辑光标、选区、当前字下划线和连词框；
- 未完整打轴时为了编辑安全而停止跨行推断。

真正值得两边共享的应是独立于编辑 UI 的“时间段 → 空间进度”核心规则。长期看，最好把这部分抽成可测试的中间算法，而不是让字幕模块逐行模仿 `paintEvent()`。

## 22. 测试覆盖差异

SUG 当前直接针对 `KaraokePreview` 的测试主要覆盖缓存与跨行依赖失效，核心 ratio/ruby 绘制缺少直接单测。

字幕模块已有较多直接测试，覆盖：

- LRC 多字等分、行内释放点、正负 offset、ruby 解析；
- `compute_char_intervals()` 与零时长；
- LTR/RTL/竖排锋面；
- 主文字 ink bounds；
- ruby 节奏驱动主文字组；
- ruby 文本匹配、全局 ruby、重复范围；
- 小假名 mora、连续时间戳停顿；
- layer 与 direct 路径像素一致性；
- Python 与 native 的若干像素 parity。

如果下一步修的是“Python 预览与 SUG 不一致”，应新增的是**跨模块对照测试**，而不是只在两边各自证明内部自洽。

## 23. 推荐的对照测试输出

每个夹具在固定时间点同时记录：

```text
SUG:
  char_wipe_times
  char_part_anchors
  seg_anchor_groups
  group_ruby_wipe
  main/ruby ratio

字幕模块:
  TimingChar.start_ms / pause_release_ms
  compute_char_intervals
  RubyAnnotation + effective ruby
  fill_segments
  main/ruby ratio
```

先比较数值，再比较 offscreen PNG。这样能立刻判断差异来自：

1. LRC 导出丢信息；
2. LRC 解析合成时间；
3. Painter ratio；
4. 布局几何；
5. 描边/发光造成的视觉错觉。

这比直接盯两块预览窗口找“哪里快了一点”可靠得多。
