# 歌词打轴子模块 `karaoke_preview` 走字逻辑

> 本文记录 StrangeUtaGame 子模块当前 Python 预览实现的实际行为，供后续修正工作台中的 Python 渲染预览使用。本文不讨论字幕渲染模块的 C++ 渲染核心，也不把预览行为直接等同于最终字幕导出行为。

## 1. 代码基线与范围

- 子模块：`krok_helper/lyrics_timing/`
- 子模块提交：`ed3758b8786b4263d803f0ed1775705153d247ac`（`SUGv1.2.6`）
- 核心文件：[`karaoke_preview.py`](../krok_helper/lyrics_timing/src/strange_uta_game/frontend/editor/timing/karaoke_preview.py)
- 上游控制器：[`timing_interface.py`](../krok_helper/lyrics_timing/src/strange_uta_game/frontend/editor/timing_interface.py)
- 数据模型：
  - [`models.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/domain/models.py)
  - [`entities.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/domain/entities.py)
- 音频显示时钟：
  - [`bass_engine.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/infrastructure/audio/bass_engine.py)
  - [`bass_tsm_engine.py`](../krok_helper/lyrics_timing/src/strange_uta_game/backend/infrastructure/audio/bass_tsm_engine.py)

`KaraokePreview` 同时承担四类职责：

1. 从音频播放位置取得当前渲染时间；
2. 把字符 checkpoint 预计算成可绘制的时间窗口；
3. 用 QPainter 绘制主文字、ruby、分色、走字裁切和编辑标记；
4. 处理滚动、选区、点击命中和预览缓存。

本文重点是前 3 类；编辑交互只记录与走字显示直接有关的部分。

## 2. 一眼看懂整体数据流

```text
Character.timestamps / sentence_end_ts
              │ Character.set_offset(offset_ms)
              ▼
global_timestamps / global_sentence_end_ts
              │
              ▼
KaraokePreview._get_sentence_render_data()
  ├─ 字符布局宽度、字形墨水边界
  ├─ linked_to_next 连词组
  ├─ char_wipe_times：每字符基础时间窗口
  ├─ char_part_anchors：单字符多 checkpoint 轴
  ├─ seg_anchor_groups：连词内“多 cp leader + cc=0 后随字”轴
  └─ group_ruby_wipe：连词 ruby 的分段时间轴
              │
              ▼
KaraokePreview.paintEvent()
  ├─ 先画未唱底色
  └─ 再按当前时间裁切叠加演唱者高亮色
       ├─ 主文字：三档优先级选择 wipe ratio
       ├─ 单字符 ruby：part 轴优先，整段线性回退
       └─ 连词 ruby：组级 piecewise 轴优先，整组线性回退
```

核心思想不是“到时间就整字变色”，而是：**先得到一个从左到右的连续进度，再只在字形实际墨水范围内裁切高亮层**。

## 3. 关键数据语义

### 3.1 `Character`

| 字段 | 预览中的意义 |
|---|---|
| `char` | 单个主文字字符 |
| `check_count` | 普通 checkpoint 数；可以为 0 |
| `timestamps` | 未加全局偏移的普通 checkpoint 时间戳 |
| `global_timestamps` | `timestamps + global offset` 后的渲染时间戳，且最小钳到 0 |
| `is_sentence_end` | 命名遗留；真实语义是演唱停顿/释放点，不是语言学句末 |
| `sentence_end_ts` | 停顿/释放时间戳 |
| `global_sentence_end_ts` | 加偏移后的停顿/释放时间戳 |
| `linked_to_next` | 当前字符是否与下一字符组成同一连词 |
| `ruby` | 可选的 `Ruby(parts=[...])` |
| `singer_id` | 本字符的演唱者及走字后颜色来源 |

普通 checkpoint 与句尾释放点是两套位置：

- 普通 checkpoint 索引范围为 `[0, check_count)`；
- 若 `is_sentence_end=True`，额外存在索引为 `check_count` 的释放点；
- `global_timestamps` 不包含释放点，`all_global_timestamps` 才包含。

### 3.2 `Ruby` / `RubyPart`

- `Ruby.text` 是全部 `RubyPart.text` 顺序拼接；
- 常规有 mora 模式要求 `len(ruby.parts) == check_count`；
- `check_count == 0` 时可以保留一整段无 mora ruby；
- `Character.push_to_ruby()` 会把时间戳和演唱者同步给 ruby，并写入各 part 相对首 checkpoint 的 `offset_ms`；
- **当前预览不读取 `Ruby.timestamps` 或 `RubyPart.offset_ms` 作为主时钟**，而是直接读取父 `Character.global_timestamps`，再用 `ruby.parts` 的文本宽度分配视觉进度。

### 3.3 三种“当前行”不要混淆

| 状态 | 用途 |
|---|---|
| `_current_line_idx` / `_current_char_idx` | 打轴编辑光标；预览指引以它为准 |
| `_last_auto_scroll_line_idx` | 根据播放时间查到的播放行 |
| `effective_current` | 当前实际用大字号和“当前行”底色绘制的行 |

播放中、自动滚动启用且未挂起时，`effective_current` 使用播放行；其他时候使用编辑光标行。滚动视口中心 `_scroll_center_line` 又是独立状态，因此“编辑光标在哪”“哪行视觉高亮”“视口滚到哪”可以不同。

## 4. 渲染时间从哪里来

### 4.1 两级刷新

`TimingInterface` 有一个 16 ms 的 `QTimer`，约 60 fps 调用：

```text
_poll_audio_position()
  → preview.set_current_time_ms(position_ms)
  → preview.update()
```

但真正进入 `paintEvent()` 时，如果正在播放且存在音频引擎，预览还会再次主动调用 `get_display_position_ms()`。因此画面使用的是绘制瞬间的较新时间，不完全依赖上一次 Qt 定时器 tick。

`get_display_position_ms()` 的行为包括：

- 扣除输出延迟；
- 播放中做单调不回退保护；
- TSM 引擎把流时间映射回原始音频时间；
- 钳制到 `[0, duration]`。

这条显示时钟与打轴按键使用的原始位置读取不是同一个语义：前者优先视觉平滑，后者优先实际打点。

### 4.2 全局偏移

真正参与计算的是每个字符的 `global_timestamps` / `global_sentence_end_ts`。控制器在载入项目或修改偏移时，会逐字符调用 `Character.set_offset(offset_ms)`。

`KaraokePreview.set_global_offset()` 自身只保存数值并清空缓存；它不会替字符重算时间戳。因此正确调用顺序是：

1. 对项目内所有 `Character` 调用 `set_offset()`；
2. 调用 `preview.set_global_offset()` 清缓存；
3. 必要时再 `set_project()` 预热缓存。

## 5. 布局宽度与“墨水边界”

预览有两个不同的水平概念。

### 5.1 布局宽度 `char_widths`

初始来自 `QFontMetrics.horizontalAdvance()`，空格则使用平均字符宽。随后可能被放大：

- 单字符宽度至少容纳自己的 ruby；
- 连词组总宽度至少容纳合并后的 ruby，并把目标总宽平均分给组内字符；
- 字符宽度至少容纳其普通 checkpoint marker；
- 句尾 marker 另在字符右侧扩展半个全角汉字宽，不混入字符本体宽度。

`char_widths` 用于：

- 整行排版与对齐；
- 字符、ruby 的居中；
- hitbox；
- 把一段时间按各字符像素宽度加权分配给多个字符。

### 5.2 墨水边界 `tightBoundingRect`

主文字与 ruby 的实际走字裁切不使用 advance box，而使用 `QFontMetrics.tightBoundingRect()`：

```text
ink_start = draw_x + tightBoundingRect.x()
wipe_width = tightBoundingRect.width() × ratio
```

这样走字锋面从字形真正起墨处开始，在最后一列墨水处结束，不会穿过左右 side bearing、ruby 居中留白或句尾 marker 扩展区。

空格、全角空格、NBSP、Tab 等通常没有墨水宽度：它们仍占布局和时间，但不会产生可见裁切。

### 5.3 一个容易忽略的结果

**时间窗口分配按布局宽度，实际变色按墨水宽度。** ruby 或 marker 把某字符布局格撑宽时，该字符会分到更多时间，但画面只在较窄的实际字形墨水内推进。

## 6. 基础字符时间窗口 `char_wipe_times`

### 6.1 起始锚点只取第一个 checkpoint

预计算先建立：

```python
start_times[char_index] = character.global_timestamps[0]
```

因此“某字符是否是 leader”只看它有没有至少一个已打时间戳；多 checkpoint 的第二、第三个时间戳在后续 part 轴中处理。

### 6.2 一行先按演唱停顿拆段

遇到 `is_sentence_end=True` 就结束一个范围。若行末没有停顿标记，剩余字符也形成最后一个范围。本文称这些范围为“演唱段”，避免把遗留字段误解成语言学句子。

每个演唱段独立找 leader 和右边界，走字不会无条件跨过已打的释放点。

### 6.3 常规 leader 段

对于演唱段中的每个 leader：

- 范围：从该 leader 到下一个 leader 前一字符；若无下一个 leader，则到演唱段末尾；
- 起点：leader 的第一个全局时间戳；
- 终点按顺序取：
  1. 同演唱段下一个 leader 的第一个时间戳；
  2. 本演唱段的 `global_sentence_end_ts`；
  3. 对行尾未设停顿的最后一段，借后续最近已打轴行的首时间戳；
  4. 再不行则用音频总时长。

只有终点存在且严格大于起点时才生成窗口。

实现上，“后续行首时间戳/音频总时长”先被预计算成一个行级 `fallback_sentence_end_ts`。它原本服务于行尾未设停顿的最后一段，但当前代码也会把它作为本行其他演唱段缺少释放时间时的最终回退；这是实际代码行为，不应误认为回退值只可能被最后一段读取。

leader 与后续没有自身时间戳的字符，共享整个 `[start, end]` 段，再按各自 `char_widths` 占比分配连续子窗口：

```text
char_start = start + duration × 前方累计宽度 / 总宽度
char_end   = start + duration × 当前累计宽度 / 总宽度
```

这就是普通 `cc=0` 字符看起来与前一个已打轴字符连续唱出的基础机制。

### 6.4 演唱段开头、首 leader 之前的字符

首 leader 前只把**从段首连续出现的 `check_count == 0` 字符**纳入“向前借锚点”逻辑；遇到第一个 `cc>0` 字符就停止。

起点来源：

- 行首演唱段：向前跨行找最近的有效末时间戳；
- 行中演唱段：取上一演唱段的 `global_sentence_end_ts`。

终点是首 leader 的第一个时间戳。起终点有效时，同样按字符布局宽度切分。

首 leader 前剩余的 `cc>0` 但尚未打轴字符走兼容旧逻辑：如果首 leader 已有窗口，就把这些字符和首 leader 一起按宽度塞进首 leader 的窗口。

### 6.5 整个演唱段没有 leader

只处理段首连续的 `cc=0` 字符，终点依次尝试：

1. 本段末尾自己的 `global_sentence_end_ts`；
2. 本行后续第一个 leader 的首时间戳；
3. 行尾回退边界。

起点必须能从上一有效行借到。起点、终点任一缺失或终点不晚于起点，就不生成走字窗口，保持底色。

### 6.6 跨行借锚点的“未完成屏障”

向前或向后扫描时，以下情况会立即停止并返回 `None`：

- 某字符 `is_sentence_end=True`，但没有 `global_sentence_end_ts`；
- 某字符 `check_count>0`，但没有任何 `global_timestamps`。

全行若所有字符都是 `cc=0` 且没有时间戳，则视为可跳过空行，继续跨行寻找。

向前扫描可把普通时间戳和句尾释放时间戳都作为候选末时间；向后扫描只取目标行第一个普通时间戳作为下一行起点。

这个屏障规则既决定走字边界，也决定哪些相邻行缓存必须随编辑一起失效。

## 7. 主文字走字的三档优先级

绘制每个有 `char_wipe_times` 的字符时，按以下顺序决定 `wipe_ratio`。

### 7.1 第一优先：`seg_anchor_groups`

这是一个专门修复“连词中，多 checkpoint leader 后跟 `cc=0` 字符”同步问题的路径。必须同时满足：

- leader 位于某个 `linked_to_next` 连词组内；
- leader 至段末都没有超出该连词组；
- leader 至少已有 2 个全局时间戳；
- leader 后至少有一个字符；
- 所有后随字符 `check_count == 0`；
- leader 的时间戳严格递增；
- 段终点晚于最后一个 checkpoint。

它把 leader 到段末所有字符的**主文字墨水宽度**拼成一条连续轴：

```text
anchors = [leader 的全部 checkpoint..., 段终点]
```

若 `ruby.parts` 数量恰好等于锚点段数，各时间段推进的总墨水比例按对应 ruby part 的 `horizontalAdvance` 加权；否则各段等分总墨水。

算出整段已走墨水后，再根据每个字符在整段墨水轴上的 offset，映射回该字符自己的 `[0, 1]`。因此锋面能跨字符连续移动，不会在 leader 的布局格结束时突然把整个 ruby 或后随字唱完。

这里拼接的是各字符 `tightBoundingRect` 的墨水宽度之和，不包含字符间布局留白和 side bearing；特殊合并段的进度轴会直接跨过这些透明区域。

### 7.2 第二优先：`char_part_anchors`

普通多 checkpoint 字符若满足：

- 至少已有 2 个 `global_timestamps`；
- 已生成基础字符窗口；
- 字符窗口终点严格晚于最后一个 checkpoint；

则建立：

```text
anchors = [ts0, ts1, ..., tsN, char_window_end]
```

主文字在每个锚点时间段内线性推进，各段对主文字总墨水的贡献**等分**，不按 ruby part 宽度加权。

例如 3 个时间段时，总体进度依次为 `0→1/3→2/3→1`，各段真实时长仍由相邻时间戳决定。

### 7.3 第三优先：整字线性

其余字符按 `char_wipe_times = (start, end)` 直接计算：

```text
ratio = clamp((current_time - start) / (end - start), 0, 1)
```

终点不晚于起点时不会进入正常渐变完成分支，属于无效/脏数据回退场景。

### 7.4 实际绘制

- `ratio == 0`：只画未唱底色；
- `0 < ratio < 1`：先画完整底色，再以墨水宽度乘 ratio 裁切叠加高亮色；
- `ratio >= 1`：直接完整画高亮色；
- 没有 `char_wipe_times`：始终画底色。

## 8. Ruby 走字

Ruby 分成“单字符 ruby”和“连词组合并 ruby”两条路径。

### 8.1 单字符 ruby 的布局

- `ruby.text` 在该字符布局格中水平居中；
- baseline 位于主文字顶部上方 `_ruby_spacing`；
- 底色与主文字使用同一个行状态底色；
- 高亮色取该字符演唱者颜色；
- 裁切范围使用整串 ruby 的 tight ink bounds。

### 8.2 单字符 ruby 的时间轴

优先使用该字符的 `char_part_anchors`。

在当前锚点段内求局部比例后：

- 若 `len(ruby.parts) == 锚点段数`，按各 part 的实际像素 advance 把段进度映射到整串 ruby；
- 若数量不匹配或 part 总宽为 0，则按段数等分；
- 最终仍用“整串 ruby 墨水宽度 × 总比例”做一个从左到右的连续 clip，并不是逐 part 分别 drawText。

若不存在 part 轴，则回退到本字符 `char_wipe_times` 的整段线性进度。

这意味着普通多 checkpoint 字符中：

- 主文字的各 checkpoint 默认等分主文字墨水；
- ruby 的各 checkpoint 在 parts 对齐时按 part 显示宽度加权；
- 两者共享时间锚点，但横向百分比不一定完全相同。

### 8.3 连词组的识别与布局

从左到右扫描字符：只要前一个字符 `linked_to_next=True`，当前字符就并入同一组。组首是 leader，其余为 non-leader。

连词组 ruby 的行为：

- 收集组内所有非空 ruby，按字符顺序拼接为一串；
- 只在组首绘制一次，non-leader 不重复绘制；
- 在整个连词组布局宽度中居中；
- 组内字符布局宽度已提前平均扩张，保证总宽至少容纳合并 ruby；
- 高亮颜色统一取**组首字符**的演唱者颜色，不按组内字符切换颜色；
- 外围绘制半透明连词框。框宽取“合并 ruby 墨水范围”和“主文字组墨水并集”中视觉更宽的一方；
- non-leader 的选区/高亮背景可能覆盖此前画出的框，所以后续字符绘制时会重画该框。

### 8.4 连词 ruby 的分段时间轴 `group_ruby_wipe`

这是连词 ruby 的首选时间模型，目标是避免把成员主文字重分配出的临时窗口边界误当成 ruby mora 边界。

先确定整组真正终点：

1. 优先取组尾字符 `char_wipe_times` 的终点；
2. 若组尾没有窗口，取组内能找到的最大窗口终点。

然后按组内字符顺序收集 ruby 时间单元：

- 成员有 ruby、有时间戳，且 `len(parts) == len(global_timestamps)`：每个 `(timestamp, part advance)` 成为一个单元；
- 有时间戳但 parts 数不匹配：把整串 ruby advance 平均分给每个时间戳；
- 成员没有自身时间戳但有 ruby：用该成员主文字窗口起点作为整串 ruby 的锚点。

全部单元随后按时间戳排序。每个单元的终点取下一单元时间戳，最后一个延伸到整组终点；累计 advance 除以总 advance，得到：

```text
(t_start, t_end, accumulated_ratio_start, accumulated_ratio_end)
```

绘制时 `_piecewise_wipe_ratio()` 具有以下语义：

- 时间尚未到首段：0；
- 位于某段：在该段累计比例区间内线性插值；
- 位于两个不连续段之间：保持上一段结束比例；
- 空文本 part 的 advance 为 0 时：消耗时间但不推进可见墨水；
- 到最后一段终点：1。

最终依旧是对合并 ruby 整串墨水做一次连续横向裁切。

### 8.5 连词 ruby 的回退

若无法构造 `group_ruby_wipe`，就用：

- 组首字符窗口起点；
- 组尾字符窗口终点；

在整段内匀速扫过合并 ruby。

## 9. 演唱者颜色、分色与未唱底色

### 9.1 颜色来源

- 每个字符优先取 `Character.singer_id` 指定的演唱者；
- 找不到有效演唱者颜色时回退项目默认演唱者；
- 再失败则使用白色；
- 连词 ruby 整组只取组首颜色；
- 当前字符下方的打轴指示线使用行级演唱者颜色。

### 9.2 分色演唱者

演唱者 `color_mode == "split"` 时可以有 2～5 个颜色。`_draw_split_text()` 把同一字形的实际墨水高度等分成水平色带，从上到下依次着色。

如果外层已经设置走字 clip，内层每个色带使用 `IntersectClip`，因此“横向走字 × 纵向分色”可以叠加。

同一主文字行的色带上下边界统一使用整行所有非空字符的墨水 top/bottom，避免不同字形高矮导致色带分界线上下跳。Ruby 则按各自整串墨水高度分色。

### 9.3 行状态底色

未唱部分不使用演唱者颜色，而按行状态选择主题色：

- 当前行：`theme.karaoke_text_current`；
- 播放行之前：`theme.karaoke_text_past`；
- 播放行之后：`theme.karaoke_text_future`。

走字本质是把演唱者色高亮层盖到这层底色之上。

## 10. “走字预览指引”不是时间走字

预览指引只在以下条件同时满足时启用：

- 设置已开启；
- 正在播放；
- 正在画 `_current_line_idx`，即打轴光标所在行。

它以 `_current_char_idx` 为唯一锚点，不看时间戳。分群规则为：

- 每个 `check_count > 0` 字符开启新群；
- 后续连续 `cc=0` 字符并入前一群；
- 行首 `cc=0` 在没有前群时自己开启一群。

当前群、上一群、下一群分别可配置开关和 alpha。命中的字符把“未走字底色”改成带透明度的演唱者分色；正常时间走字仍在其上以不透明高亮覆盖。

因此它只是打轴时的视觉提示：

- 尚未唱到时能隐约看到前/中/后群；
- 唱到的部分仍按真实时间逐步覆盖；
- 完全唱完后仍是正常不透明演唱者色。

连词组合并 ruby 只查询组首字符的指引 alpha；若指引只命中 non-leader，合并 ruby 不会单独读取该 non-leader 的 alpha。

## 11. 自动滚动与走字是两条独立逻辑

`set_playing(True)` 会按每行 `global_timing_start_ms` 建立 `(switch_time, line_index)` 快照，并排序。播放时间通过 `bisect_right` 以 O(log n) 查找当前播放行。

需要注意：

- 没有任何时间戳的行不进入快照；
- 时间早于第一个快照点时，查找结果仍被钳到第一个已打轴行；
- 快照在进入播放态时重建，不会因每一帧绘制自动重建；
- 自动滚动只决定视觉行和视口，不参与字符/ruby 的 wipe ratio；
- 用户滚轮、拖动滚动条或编辑操作可以挂起自动滚动，冷却后再恢复。

## 12. 缓存与失效

`_get_sentence_render_data()` 的结果按行缓存，内容包括：

- 字符布局宽度和墨水边界；
- 句尾 marker 扩展宽度；
- `char_wipe_times`；
- 连词组映射；
- 单字 part 锚点；
- 合并主文字锚点组；
- 单字/连词 ruby 墨水边界；
- 连词 ruby piecewise 时间轴。

缓存键实际由以下信息共同决定：

- 行索引；
- 行版本号；
- 全局版本号；
- 字体种类键 `cur` / `ctx`。

每行缓存槽只有一个，因此同一行在当前行字体和上下文行字体之间切换时会覆盖并重算。

### 12.1 为什么改一行会失效邻行

某行可能向前借上一有效行的末时间戳，也可能向后借下一有效行的首时间戳。中间若隔着任意数量“全 `cc=0` 且无时间戳”的可跳过行，依赖半径就不止一行。

`_invalidate_line_and_dependents()` 因而按与真实扫描一致的屏障规则向前、向后扩散失效，直到遇到能提供时间或阻断扫描的行。

字体、marker、项目等全局变化会清空缓存或增加全局版本。播放期间还会以当前播放/编辑行为中心，每次最多预热少量邻近行，降低首帧卡顿。

## 13. 典型场景

### 13.1 普通逐字

```text
字 A: ts=1000
字 B: ts=1500
句尾释放: 2200
```

- A 在 1000～1500 ms 走完；
- B 在 1500～2200 ms 走完；
- 主文字和无多 part 的 ruby 都在各自窗口内线性推进。

### 13.2 leader 后跟 `cc=0`

```text
A: ts=1000, cc=1
B: 无 ts, cc=0
下一 leader: ts=2000
```

A、B 按布局宽度瓜分 1000～2000 ms，而不是 A 用完整 1000 ms、B 瞬间补完。

### 13.3 单字多 checkpoint

```text
主字: checkpoints=[1000, 1300]
窗口终点: 2000
ruby.parts=["きょ", "う"]
```

- 时间段为 1000～1300、1300～2000；
- 主文字每段各推进一半墨水；
- ruby 按“きょ”和“う”的显示宽度分配两段推进量；
- 两层的时间边界一致，但相同时间点的横向百分比可能不同。

### 13.4 连词熟字训

```text
「明」 linked_to_next=True, ruby="あした", 多 checkpoint
「日」 cc=0
```

- ruby 只在“明”这一轮合并绘制一次；
- 主文字若满足 `seg_anchor_groups` 条件，会把“明日”的墨水拼成整段推进；
- 连词 ruby 按实际 mora 时间戳和 part 宽度推进到组尾真实结束时间；
- 不再把“明”临时分到的字符窗口终点误当成「あした」整串终点。

### 13.5 行首 `cc=0`

行首连续 `cc=0` 字符只有在能从上一有效行借到末时间、并能在本行找到有效终点时才走字。若中间遇到一行“有 checkpoint 但尚未打轴”，扫描被阻断，当前行首保持底色。

## 14. 当前实现中值得留意的边界与维护点

以下是代码当前行为，不一定都是缺陷，但后续修改预览小问题时应先确认产品预期。

1. **主文字多 part 与 ruby 多 part 的空间权重不同。**普通单字主文字按段数等分，ruby 按 part 像素宽度加权；只有特殊连词合并段会让主文字也采用 ruby part 权重。
2. **时间分配宽度会被 ruby/marker 撑大。**这影响 `cc=0` 字符瓜分时间，但裁切只扫字形墨水。
3. **连词 ruby 使用组首演唱者颜色。**组内变更 singer 不会在合并 ruby 内分色切换。
4. **连词 ruby 的指引 alpha 只看组首。**光标落在 non-leader 群时可能与主文字提示不完全一致。
5. **普通 `char_part_anchors` 不显式校验全部时间戳严格递增。**特殊 `seg_anchor_groups` 有严格递增检查；普通路径主要依赖数据层保证合法。
6. **组级 ruby 单元会按时间戳排序。**若原数据顺序与时间顺序冲突，视觉顺序仍是整串从左向右，但各段时序按排序结果执行。
7. **自动滚动快照与走字缓存不同步重建。**时间戳编辑会失效渲染缓存，但行切换快照只在进入播放态时建立。
8. **`KaraokePreview.set_global_offset()` 不负责更新字符。**遗漏 `Character.set_offset()` 会让字段值和实际走字时间不一致。
9. **首个播放行会提前成为视觉当前行。**时间早于第一条行切换点时，二分结果仍钳到第一个已打轴行；这不代表其字符已经开始走字。
10. **空白字符消耗时间但没有可见锋面。**这会形成视觉停顿，是按当前墨水裁切设计自然产生的结果。

## 15. 测试现状

当前直接针对 `KaraokePreview` 的单元测试主要在：

`krok_helper/lyrics_timing/tests/unit/frontend/test_karaoke_preview_cache.py`

已覆盖重点是：

- 位置/focus 变化不应误伤布局缓存；
- 单行失效版本推进；
- 跨可跳过空行的双向依赖失效；
- 遇到有效时间行或未完成屏障时停止扩散；
- 扩散到列表边界。

目前没有看到对以下核心视觉算法的直接单元测试：

- `_anchor_ratio()` / `_anchor_part_ratio()`；
- `_piecewise_wipe_ratio()`；
- `char_wipe_times` 的各种边界构造；
- 单字 ruby 与主文字多 checkpoint 同步；
- 连词 ruby 分段轴；
- QPainter 墨水裁切结果。

后续若修 Python 预览走字，最稳妥的测试切入点是先把 `_get_sentence_render_data()` 的纯数据结果和几个 ratio helper 参数化测试补齐，再做少量 offscreen 像素级绘制测试。

## 16. 修改时的建议定位顺序

若问题表现为“时间不对”，依次检查：

1. `Character.global_timestamps` / `global_sentence_end_ts`；
2. 演唱段拆分与跨行借锚点；
3. `char_wipe_times`；
4. 是否进入 `char_part_anchors` 或 `seg_anchor_groups`；
5. `paintEvent()` 中最终选中的 ratio 分支。

若问题只发生在 ruby：

1. 确认单字还是 `linked_to_next` 连词组；
2. 检查 `ruby.parts` 数与实际时间戳数；
3. 单字看 `char_part_anchors`；
4. 连词看 `group_ruby_wipe` 与组尾 `char_wipe_times`；
5. 最后检查 tight ink bounds 和 clip 宽度。

若问题表现为“走字时快时慢或在空白处停顿”，同时对照：

- 用于分配时间的 `char_widths`；
- 用于实际着色的 `char_ink_widths` / ruby ink width；
- ruby part 使用的是 `horizontalAdvance` 权重，而不是 tight ink width。

这三套宽度语义不同，是当前实现中最容易造成“代码算得对，但肉眼觉得不同步”的地方。
