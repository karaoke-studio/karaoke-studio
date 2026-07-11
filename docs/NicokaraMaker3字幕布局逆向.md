# NicokaraMaker3 字幕布局设置逆向

本文记录 NicokaraMaker3 10.74.80.0 中与字幕布局相关的全部已确认设置项、默认值、UI 语义和实际排版行为，供工作台第 5 步「字幕视频生成」对齐使用。

逆向来源：

- 本机软件：`D:\カラオケ\NicokaraMaker3_10.74.80.0_x64\NicokaraMaker3_10.74.80.0_x64\NicoKaraMaker3.dll`
- 反编译工具：`ilspycmd`
- 官方帮助页：`https://shinta.coresv.com/help/NicoKaraMaker3_JPN.html` 的「レイアウト設定パネル」
- 用户给出的布局页截图

## 设置层级

N3 的布局不是单个全局配置，而是三层：

| 层级 | 数据位置 | 作用 |
|---|---|---|
| 布局定义 | `ProjectDataModel.LyricsLayouts: ObservableCollection<LyricsLayoutModel>` | 保存多个「レイアウト設定」，每个设置定义一套垂直/水平/间距/ruby 布局规则 |
| 行应用 | `LyricsLineInfo.LayoutIndex` / `TitleInfoModel.LayoutIndex` | 指定歌词页或标题使用哪一个布局定义 |
| 自动应用 | `LayoutSelector` add-on | 按页行数或统一布局批量改写 `LayoutIndex` |

因此「布局相关所有设置」应包含：

- `LyricsLayoutModel` 本体字段；
- 布局设置的增删、复制、模板同步；
- 歌词/标题行上的 `LayoutIndex`；
- 自动布局选择器 `LinesLayoutSelector` / `UnificationLayoutSelector`；
- 影响布局警告的左右余白检查；
- 与布局强相关的字体几何和 ruby 计算规则。

## Layout 设置本体

`LyricsLayoutModel` 是布局页的核心模型。用户可见的每个「レイアウト設定 N」对应一个实例。

| N3 字段 | UI 名称 | 类型/枚举 | 默认值 | 实际语义 |
|---|---|---|---:|---|
| `SettingsName` | レイアウト設定 N の名称 | string | 由预设决定 | 布局设置名称，可复制/改名/模板保存 |
| `SelectedVerticalAlignmentIndex` | 上下配置 | `VerticalLayoutAlignment`：`0=Top` / `1=Middle` / `2=Bottom` | `2` | 整页纵向锚点。Top 用上余白；Bottom 用下余白；Middle 居中且上下余白控件隐藏 |
| `LineSpace` / `LineSpaceSize` | 行間 | `SizeAndRatio` / int px | `85` | 主文字行盒之间的间距，可为负。N3 官方说明强调这是「親文字と親文字」间隔，不是 ruby 间隔 |
| `SmartHorizon` | スマート水平配置 | `SmartHorizon`：`0=None` / `1=Single` / `2=Multi` | `2` | 左/右对齐的短行自动向中央收拢；单行页只要不是 `None` 就直接居中 |
| `VerticalMargin` / `VerticalMarginSize` | 上余白 / 下余白 | `SizeAndRatio` / int px | `50` | Top 时为视频上端到最上行的余白；Bottom 时为视频下端到最下行的余白；Middle 忽略 |
| `HorizontalMargin` / `HorizontalMarginSize` | 左右余白 | `SizeAndRatio` / int px | `50` | Left 行左边贴 `margin`，Right 行右边贴 `width - margin`；Center 行居中，不以 margin 为锚 |
| `HorizontalAlignments` | 行ごとの左右レイアウト | list of `HorizontalLayoutAlignmentModel` | 由预设决定 | 每个页内行的左/中/右对齐列表。列表长度也是该布局希望显示的行数 |
| `LyricsInterval` / `LyricsIntervalSize` | 歌詞間隔 | `SizeAndRatio` / int px | `0` | 正文字间距；可为负，负数会让文字重叠 |
| `AllowBiting` | 一部の文字の食い込みを許容する | bool | `false` | 是否保留字体自身负 side bearing。关闭时负 side bearing 被钳为 0；开启后即使 `LyricsInterval >= 0` 也可能咬合 |
| `RubyInterval` / `RubyIntervalSize` | ルビ間隔 | `SizeAndRatio` / int px | `0` | ruby 内部字符最小间距；也用于相邻 ruby 之间的避让间距。官方说明允许负值 |
| `RubyAlignment` | ルビ配置 | `RubyAlignment`：`0=Auto` / `1=Center` / `2=EqualSpace` | `0` | ruby 相对标注正文范围的水平排布方式 |
| `LyricsAndRubyInterval` / `LyricsAndRubyIntervalSize` | 歌詞とルビの間隔 | `SizeAndRatio` / int px | `0` | 正文行盒与 ruby 行盒的垂直间距。ruby 下缘放在 `正文 DrawTop - interval` |

内部/UI 辅助字段：

| 字段 | 用途 |
|---|---|
| `SelectedHorizontalAlignmentIndex` | 当前在「行ごとの左右レイアウト」列表中选中的项，仅 UI 状态 |
| `VerticalMarginVisibilityTop` / `VerticalMarginVisibilityBottom` | 根据 `SelectedVerticalAlignmentIndex` 显示上余白或下余白输入框，不是持久化布局语义 |
| `Synchronize` / `Guid` / `Notification` | 继承自 `SettingsNameModel`，用于模板同步、引用识别、脏标记 |

## SizeAndRatio 缩放规则

布局中的像素值大多不是只存一个 int，而是 `SizeAndRatio`：

```text
Size      当前像素值
Reference 创建或上次更新时的视频高度
Ratio     Size / Reference
```

创建时：

```text
Size = 指定 px
Reference = MovieInfo.Height
Ratio = Size / Reference
```

当视频高度变化时：

```text
Size = (int)(newReference * Ratio)
Reference = newReference
```

注意 `(int)` 是向 0 截断。`Ratio == 0` 时不会按高度重算，继续保持 `Size` 为 0。

本项目如果要兼容 N3 工程语义，布局字段最好同时保留「当前 px」和「相对输出高度比例」，至少在视频/输出高度变化时按同样规则更新。

## 默认内置布局

`ProjectDataModel.AddDefaultLyricsLayoutsIfNeeded()` 会补齐 6 个布局预设，创建逻辑在 `CreateLyricsLayout(index)`。

通用默认：

```text
SelectedVerticalAlignmentIndex = Bottom
LineSpace = 85
VerticalMargin = 50
HorizontalMargin = 50
SmartHorizon = Multi
LyricsInterval = 0
AllowBiting = false
RubyInterval = 0
RubyAlignment = Auto
LyricsAndRubyInterval = 0
HorizontalAlignments 初始包含 [Left]
```

各预设差异：

| index | 名称 | 上下配置 | 行間 | 上/下余白 | 左右余白 | 行ごとの左右レイアウト |
|---:|---|---|---:|---:|---:|---|
| 0 | 下寄せ2行 | Bottom | 85 | 50 | 50 | Left, Right |
| 1 | 下寄せ3行 | Bottom | 85 | 50 | 50 | Left, Center, Right |
| 2 | 上寄せ2行 | Top | 85 | 50 | 50 | Left, Right |
| 3 | コーラス | Bottom | 85 | 505 | 50 | Center |
| 4 | タイトル左上 | Top | 15 | 50 | 50 | Left |
| 5 | タイトル中央 | Middle | 30 | 50 | 200 | Center, Right |

`TitleInfoModel` 新建时会优先选择名称包含「タイトル」的第一个布局，因此默认标题通常使用「タイトル左上」。

## 上下配置与行号映射

`HorizontalAlignments` 的索引如何映射到页面内行，取决于上下配置：

| 上下配置 | 映射方式 | 超出列表长度时 |
|---|---|---|
| Top / Middle | 从页首行向下取 `HorizontalAlignments[0..]` | 多出来的下方行继续使用最后一项 |
| Bottom | 从页底行向上倒着取列表末尾 | 多出来的上方行继续使用第一项 |

例：

- `Bottom + [Left, Right]`：页内 2 行时上行 Left、下行 Right；
- `Bottom + [Left, Center, Right]`：页内 3 行时上行 Left、中行 Center、下行 Right；
- 如果 `Bottom + [Left, Right]` 被用于 3 行页，则最上方溢出的行仍用 Left。

「行ごとの左右レイアウト」的增删规则：

| 操作 | Bottom 布局 | Top / Middle 布局 |
|---|---|---|
| 添加一行左右布局 | 在列表开头插入当前第一项的复制，并选中 0 | 在列表末尾追加当前最后一项的复制，并选中末尾 |
| 删除 | 删除当前选中项 | 删除当前选中项 |
| 限制 | 至少保留 1 项 | 至少保留 1 项 |

这个设计保证 Bottom 布局新增行时是在「上方」加一行，Top/Middle 则是在「下方」加一行。

## 水平排版规则

实际输出路径在 `OutputDrawDataGenerator.SetOneLineX()`。

| 行对齐 | 初始放置 |
|---|---|
| Left | `DrawLineLeft()` 移到 `HorizontalMargin.Size` |
| Center | 以整行自然宽度居中，约为 `(movieWidth - lineRightFromOrigin) / 2` |
| Right | `DrawLineRight()` 移到 `movieWidth - HorizontalMargin.Size` |

`DrawLineLeft()` 会考虑行首 ruby 的左溢出；`DrawLineRight()` 会取正文末尾和末尾 ruby 右缘的最大值。因此 ruby 可能影响整行左右边界。

预览/输出会检查左右余白：

| 条件 | 级别 | 文案语义 |
|---|---|---|
| 行左边 < 0 或行右边 > 视频宽 | Warning | 字幕从视频中溢出 |
| 行未溢出，但左边 < `HorizontalMargin` 或右边 > `width - HorizontalMargin` | Information | 字幕的左右余白无法确保 |

Center 行虽然不按左右余白锚定，仍会被余白检查提示。

## 垂直排版规则

实际输出路径在 `OutputDrawDataGenerator.SetOneLineY()`。排版以一页的 `topLineIndex..bottomLineIndex` 为单位。

| 上下配置 | 第一条基线/行盒位置 |
|---|---|
| Top | 最上行 `DrawTop = VerticalMargin.Size`，后续行向下累加 `上一行 DrawHeight + LineSpace` |
| Middle | 先求整页高度：所有正文行 `DrawHeight` 之和 + `(页行数 - 1) * LineSpace`，再整体垂直居中 |
| Bottom | 最下行 `DrawBottom = movieHeight - VerticalMargin.Size`，上方行向上递减 `下一行 DrawHeight + LineSpace` |

特殊规则：

- `LineSpace` 可为负，负数会让行盒重叠；
- 单行页在 Bottom 模式下会根据相邻页重叠情况决定是否强制占最下行位置，否则可能上移一行高度加行距，以避免与上一页/下一页显示逻辑冲突；
- ruby 在正文 y 放置后处理，`LyricsAndRubyInterval` 不参与主行之间的 `LineSpace` 计算。

## SmartHorizon 语义

`SmartHorizon` 只在整页基础布局完成后作为二次水平修正。

这里的“整页”严格取 `MultiTextInfoModel.TopLineIndex/BottomLineIndex`：
`PageBreak` 与 `ParagraphBreak` **都会**结束当前页。两者之间即使只有一条歌词，
也是真正的单行页；反之，`ParagraphBreak` 前的最后一条歌词若与前一条同处边界内，
仍属于多行页，不能因为它同时是“段落末行”就单独居中。导入 `.n3proj` 时必须保留
`LineInfos` 的显式 break，不能只按布局行数机械分组。

直接加载 LRC 时，N3 默认使用 `SeqLinesBreaker`（`Seq=2`）重建这些 break：

1. 从当前候选行起向后最多取 `Seq` 行，求最早演唱开始；
2. 从当前页首到上一行求最晚演唱结束；
3. 两者间隔达到 `PreTime2 + PostTime2 + IntervalTime2`（默认
   `1800 + 1000 + 300 = 3100ms`）时，优先在当前行前插入 `ParagraphBreak`；
4. 否则当前页累计达到 `Seq` 行时插入 `PageBreak`；两种 break 都重置页内计数。

注意第 1 步会向后看满一页，不是只比较相邻两行。这正是 Marginality 中
“真っ白な頁…”前判为 `PageBreak`、下一句前判为 `ParagraphBreak` 的原因。

| 值 | UI | 行为 |
|---|---|---|
| `None` | 調整しない | 不做二次修正；Left/Right 永远贴指定左右余白 |
| `Single` | 中心位置揃え | 多行页中，对每一条非 Center 的短行单独判断，过短则向中央靠拢；单行页直接居中 |
| `Multi` | 左右余白揃え | 多行页中，只有页面同时存在 Left 和 Right 行时才整体修正；单行页直接居中 |

单行页：

```text
if SmartHorizon != None:
    行整体居中
```

`Single` 多行页近似规则：

```text
size = HorizontalMargin
font = 正文字号
lineWidth = DrawLineRight - DrawLineLeft
thresholdLeft = movieWidth / 2 + font / 2 - lineWidth

if thresholdLeft > size:
    Left 行起点 = thresholdLeft
    Right 行起点 = movieWidth / 2 - font / 2
```

也就是说，短的左对齐行会从靠近中心的位置开始；短的右对齐行会在靠近中心的位置结束。Center 行不受影响。

`Multi` 多行页近似规则：

```text
leftMax   = 页面内所有 Left 行最大宽度
centerMax = 页面内所有 Center 行最大宽度
rightMax  = 页面内所有 Right 行最大宽度
font      = 正文字号
slack = movieWidth - horizontalMargin * 2 - leftMax - centerMax - rightMax + font

if 页面没有 Left 或没有 Right: 不修正
if slack <= 0: 不修正
Left 行整体右移 slack / 2
Right 行整体左移 slack / 2
Center 行不动
```

官方帮助里的「中心位置揃え」对应 `Single`，「左右余白揃え」对应 `Multi`。截图中的「左右余白揃え」就是 `SmartHorizon.Multi`。

## 正文字步进与咬合

N3 不使用“上一字墨水右缘到下一字墨水左缘固定距离”的模型。它先从 DirectWrite 字形轮廓取得墨水边界，再结合字体 design glyph metrics 计算每个字自己的布局宽度。

对普通字形，忽略缩放单位后可写成：

```text
布局宽度 = int(墨水轮廓宽度) * (左 side bearing + advance + 右 side bearing) / advance
左偏移   = int(墨水轮廓宽度) * 左 side bearing / advance
字步进   = 布局宽度 + 描边宽度 + LyricsInterval
```

N3 不会直接把字体轮廓放在字符框起点。它先把轮廓自身的 `bounds.Left` 归零，再按下面的位置绘制：

```text
轮廓墨水左缘 = 字符框起点 + 左偏移 + 第一描边宽度 / 2
```

第一描边完成后，其外缘从「字符框起点 + 左偏移」开始。这个重新定位步骤很重要：布局宽度与左偏移必须同时复刻，只复刻宽度会令窄字形在字符框内偏向一侧。

`AllowBiting=false` 时，负的左右 side bearing 会先被钳制为 0；`AllowBiting=true` 时保留负值。因此 `LyricsInterval=0` 也不等于墨水之间为 0 px，实际可见间隙仍随每个字形的轮廓和 side bearing 改变。

空格不走普通字形公式。N3 的全局 `SpaceWidth` 默认是字号的 20%；无轮廓的其他字符使用另一条兜底比例公式。

## Ruby 布局

ruby 先按自身布局宽度和 `RubyInterval` 排列，再相对于所标注的正文范围放置。

`RubyAlignment`：

| 值 | UI | 行为 |
|---|---|---|
| `Auto` | 自動配置 | 正文范围或 ruby 全部为英数字时走 `Center`，否则走 `EqualSpace` |
| `Center` | センタリング | ruby 整组在正文范围中居中 |
| `EqualSpace` | 均等割り付け | 将剩余空间分到 ruby 字符间/两侧，且间距不小于 `RubyInterval` |

`EqualSpace` 公式：

```text
ruby 自然宽度 = sum(每个 ruby 字符 DrawWidth)

if 正文范围宽度 <= ruby 自然宽度:
    字间距 = (正文范围宽度 - ruby 自然宽度) / (ruby 字数 - 1)
else:
    字间距 = (正文范围宽度 - ruby 自然宽度) / (ruby 字数 + 1)

字间距 = max(字间距, RubyInterval)
ruby 起点 = 正文左缘 + (正文范围宽度 - (ruby 自然宽度 + 字间距 * (ruby 字数 - 1))) / 2
```

因此当 `ひかり` 这类 ruby 比单个汉字更宽时，N3 会允许 ruby 起点落到正文左缘左侧，让整组 ruby 围绕正文范围居中溢出；不会把它硬钉在正文左缘。默认 `Auto` 对日文正文/ruby 会走 `EqualSpace`。

相邻 ruby 避让：

```text
if 当前 ruby 左缘 - (前一个 ruby 右缘 + RubyInterval) < 0:
    从当前正文字符开始，后续整行向右移动
```

长 ruby 与正文避让：

N3 还有环境级选项 `IsolateLongRubyKanji`。开启时，会比较「前一个正文/ruby 组合右缘」与「当前正文/ruby 组合左缘」，若间距小于 `LyricsInterval`，从当前正文字符开始向右推开。这个开关不是布局页字段，但会改变最终布局。

垂直方向：

```text
ruby 行盒下缘 = 正文 DrawTop - LyricsAndRubyInterval
```

所以默认 `LyricsAndRubyInterval=0` 表示 ruby 行盒与正文行盒相接，不表示两个墨水轮廓贴住，也不保证肉眼可见间隙为 0。

## 歌词/标题如何应用布局

### 歌词行

歌词设置表格里的「レイアウト」最终写入 `LyricsLineInfo.LayoutIndex`。

实际修改语义：

- 布局不能应用到非歌词/非标题行；
- 如果当前行属于一个页面，改布局会把同一页 `TopLineIndex..BottomLineIndex` 内的所有歌词行一起改成同一个 `LayoutIndex`；
- 多选行时，对所有选中项执行同样应用；
- 不能按字符局部指定布局。官方帮助也说明布局对整行生效，同一页内行会联动。

### 标题行

标题设置使用 `TitleInfoModel.LayoutIndex`。预览/输出时会把标题生成的 `LineInfos[j].LayoutIndex` 统一设为该标题设置的 `LayoutIndex`。

默认标题设置创建逻辑：

```text
取第一个 SettingsName 包含 "タイトル" 的 LyricsLayoutModel
TitleInfoModel.LayoutIndex = 该布局 index
```

## 自动布局选择器

N3 的「自動でレイアウトを設定」窗口有两个内置 add-on。它们会批量改写歌词行 `LayoutIndex`，不直接修改 `LyricsLayoutModel` 本体。

### 行数に応じてレイアウトを設定

类：`LinesLayoutSelector`

UI 设置：

| 字段 | UI | 说明 |
|---|---|---|
| `SelectableBegin` / `SelectedBeginIndex` | 適用対象レイアウト 起点 | 可选布局范围起点 |
| `SelectableEnd` / `SelectedEndIndex` | 適用対象レイアウト 终点 | 可选布局范围终点 |

范围修正：

- 起点无效时用 0；
- 终点无效时，从起点开始向后扩展，直到下一个布局的 `SelectedVerticalAlignmentIndex` 与起点不同为止；
- 起点大于终点时交换。

选择规则：

```text
pageLines = 当前页歌词行数

先在 [begin, end] 范围内找第一个 HorizontalAlignments.Count == pageLines 的布局
找不到则 pageLines - 1, pageLines - 2 ... 继续找
仍找不到则使用 begin
```

同一行数会缓存选择结果。默认 2 行页会命中「下寄せ2行」，3 行页会命中「下寄せ3行」。

### すべて同じレイアウトにする

类：`UnificationLayoutSelector`

UI 设置：

| 字段 | UI | 说明 |
|---|---|---|
| `Layout` / `SelectedLayoutIndex` | 使用するレイアウト | 所有歌词行统一应用的布局 |

选择规则：

- 选中的布局无效时回退到 index 0；
- 遍历所有歌词行，`Kind == Lyrics` 时统一设置为该布局 index。

## 自动运行相关设置

这些不是布局页本体设置，但会影响布局何时被自动重算：

| 字段 | 位置 | 默认 | 作用 |
|---|---|---:|---|
| `SettingsAddOnSequence.LayoutSelectorId` | 每个歌词源的 `LastSelectedAddOns` | 空时回退到第一个 LayoutSelector | 记录上次使用的自动布局选择器 |
| `Nkm3Settings.RunLayoutSelector` | 环境设置 | `true` | 执行「自动设置」链条时是否运行布局选择器 |
| `Nkm3Settings.ReloadRunLayoutSelector` | 环境设置 | 未在布局模型中定义 | 重新读取歌词文件后是否重新运行布局选择器 |

典型流程：导入/重新读取歌词后，N3 会按环境设置决定是否依次运行分割、字体、布局、显示时刻、字幕动作选择器。

## 布局设置管理与模板

布局页工具菜单包括：

| 操作 | 行为 |
|---|---|
| レイアウト設定を追加 | 复制当前布局，在当前 tab 后插入；新布局生成新 `Guid`，名称自动避重 |
| レイアウト設定を削除 | 删除当前布局；至少保留 1 个布局；删除后会重写歌词行和标题的 layout index |
| テンプレートを使用 | 从布局模板载入一个 `LyricsLayoutModel`，按当前视频高度调用 `UpdateSizes()` 后插入当前 tab |
| テンプレートとして登録 | 将当前布局作为模板同步/保存 |
| tab 拖拽排序 | 调整 `LyricsLayouts` 顺序，同时重写歌词行和标题的 layout index |

模板相关常量中 `TemplateKind.Layout` 的显示名是「レイアウト設定」。载入模板时如果当前工程中已有相同 `Guid` 的布局，会提示已使用，而不是重复插入。

## 与本项目 Style 的对应建议

| N3 | 工作台建议字段 | 备注 |
|---|---|---|
| `SelectedVerticalAlignmentIndex` | `vertical_align` | 需要支持 top / middle / bottom |
| `LineSpaceSize` | `line_gap_px` | 当前双行 `line_gap_px` 应按主文字行盒解释 |
| `SmartHorizon` | `smart_horizontal_layout` | 需区分 none / center-position / equal-margins |
| `VerticalMarginSize` | `vertical_margin_px` 或 top/bottom margin | top/bottom 根据 vertical_align 切换 |
| `HorizontalMarginSize` | `horizontal_margin_px` | 同时用于 warning |
| `HorizontalAlignments` | `line_alignments` | 不应只固定 2 行；N3 支持任意行数列表 |
| `LyricsIntervalSize` | `char_gap_px` | 允许负数 |
| `AllowBiting` | `allow_font_bearing_bite` | 默认 false |
| `RubyIntervalSize` | `ruby_char_gap_px` | 与 `ruby_gap_px` 不同 |
| `RubyAlignment` | `ruby_alignment` | auto / center / equal_space |
| `LyricsAndRubyIntervalSize` | `ruby_gap_px` | 正文与 ruby 行盒间距，默认 0 |
| `LayoutIndex` | per-line/page layout id | 应用于整页，不能只改单行中的一个字符 |

## “めくるめく権謀”的间距现象

使用旧默认字体 `UD Digi Kyokasho N-B`、正文字号 100 px、描边 9 px 时，早期实现（仅复刻布局宽度、尚未应用 N3 左偏移）得到的可见墨水间隙约为：

| 相邻字 | 可见墨水间隙 |
|---|---:|
| め → く | 36.6 px |
| く → る | 27.3 px |
| る → め | 23.1 px |
| め → く | 36.6 px |
| く → 権 | 13.2 px |
| 権 → 謀 | 14.4 px |

`く` 的墨水明显窄，且左右 side bearing 不对称；布局宽度会被压缩到约 81 px，而宽汉字约为 107-108 px。但上表不能作为 N3 最终像素结果，因为 N3 还会应用 `CharGeometryLeftOffset` 重新放置墨水轮廓。当前实现若漏掉该偏移，局部视觉间距仍会与 N3 不一致。

以「寄り添って」为例，`り` 的左 side bearing 很大。若 QPainter 原样保留该留白，会令 `り` 的墨水偏向字符框右侧；N3 会把左留白按轮廓宽度重新缩放后作为左偏移，使 `寄→り` 与 `り→添` 的描边外缘间距更接近一致。

## 发光公式（2026-07-11 补充逆向）

N3 的 `SubtitleAction.DrawOneLineDecorBlurMulti()` 对每行执行
`BlurLevel + 1` 次发光。设 `R = int(DecorSize.Size * Scale)`、
`N = BlurLevel + 1`，第 `i` 次（从 0 开始）的参数为：

```text
sigma_i = R - floor(i * R / N)
```

因此 `R=13` 时低/中/高三档分别是 `(13)`、`(13, 7)`、
`(13, 9, 5)`。每次都重新清空工作位图，先以装饰色绘制轮廓，再把
`sigma_i` 直接赋给 Vortice/Direct2D `GaussianBlur.StandardDeviation`，最后以
SourceOver 叠加到目标。Direct2D 默认使用 soft border，理论核支撑半径为 `3σ`：

```text
w(x) = exp(-(x*x) / (2*sigma*sigma)),  x in [-ceil(3*sigma), ceil(3*sigma)]
w = w / sum(w)
```

发光源轮廓宽度也不是普通描边宽度：

- 未启用二重描边：`(EdgeSize + DecorSize) * Scale`；
- 启用二重描边：`(EdgeSize + EdgeSize2 + DecorSize) * Scale`，此时一重描边的
  blur 绘制会提前返回，只使用合并后的二重轮廓；
- 使用 `DecorBefore` / `DecorAfter` 画刷，正文填充不进入 blur 工作位图。

工作台不能把 `sigma_i` 直接传给 Qt `QGraphicsBlurEffect.blurRadius`：Qt 的 raster
实现会先乘 `2.5`，再使用指数模糊而非上述高斯核，中心 alpha 分布与 N3 明显不同。

另一个容易遗漏的细节是 N3 没有设置 `GaussianBlur.Optimization`，因此使用
Direct2D 默认的 `Balanced`，不是直接计算上面的离散核。微软定义该模式会先做
pre-scaling，再用 trilinear filtering 恢复尺寸。对 `Dark spiral journey/1.n3proj`
的默认样式（`DecorSize=10`、`BlurLevel=0`、`EdgeSize=5`）做 BGRA8 实测：15px
竖直发光源的中心 alpha 为 139；直接离散高斯为 140，多个内圈像素高 1–2，整图
alpha 总和高约 0.9%。工作台现对半径不小于 8px 的发光先做半尺寸平滑预缩放，
以同比例 σ 模糊后再平滑恢复；上述探针 34 个采样点与 N3 的误差均不超过 1 alpha，
总 alpha 误差约 0.12%。小半径仍使用直接 `3σ` 可分离高斯，避免无谓的缩放损失。

## 实现优先级建议

如果要把 N3 布局语义并入工作台，推荐顺序：

1. 先补齐 `LyricsLayoutModel` 等价字段：上下配置、行距、上下/左右余白、每行对齐列表、三种 SmartHorizon、三种 ruby 对齐。
2. 将现有双行模式升级为「任意行数布局列表」，Bottom 模式从下向上映射。
3. 实现 `SizeAndRatio` 式输出高度缩放，保证换分辨率时布局一致。
4. 把 `LayoutIndex` 从单行概念提升为「页级联动」概念。
5. 最后细化 DirectWrite 字形布局宽度、side bearing 咬合、ruby 反推正文避让等像素级规则。
