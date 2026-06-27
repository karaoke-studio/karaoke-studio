# NicokaraMaker3 字幕布局逆向

本文记录 NicokaraMaker3 10.74.80.0 的歌词布局模型、默认值和关键计算方式，供字幕渲染模块对齐使用。

## 布局参数

`LyricsLayoutModel` 暴露的参数如下：

| N3 参数 | 含义 | 默认值 |
|---|---|---:|
| `SelectedVerticalAlignmentIndex` | 整页垂直对齐：上 / 中 / 下 | 下（2） |
| `LineSpace` | 行间距 | 85 |
| `SmartHorizon` | 多行智能水平排布：无 / 单行 / 多行 | 多行（2） |
| `VerticalMargin` | 上下边距 | 50 |
| `HorizontalMargin` | 左右边距 | 50 |
| `HorizontalAlignments` | 各行水平对齐：左 / 中 / 右 | 由布局预设决定 |
| `LyricsInterval` | 正文字间距 | **0** |
| `AllowBiting` | 是否允许负 side bearing 令文字咬合 | **否** |
| `RubyInterval` | 同一组假名中，假名字符之间的最小间距 | **0** |
| `RubyAlignment` | 假名水平对齐：自动 / 居中 / 均分 | 自动（0） |
| `LyricsAndRubyInterval` | 正文与假名的垂直间距 | **0** |

这里有两个容易混淆的量：`RubyInterval` 是假名内部的字间距；`LyricsAndRubyInterval` 才是正文与假名之间的距离。本项目的 `Style.ruby_gap_px` 对应后者。

这些距离使用 `SizeAndRatio(Size, Reference, Ratio)` 保存。参考高度变化时，N3 按 `Size = (int)(newReference * Ratio)` 缩放，即正值直接截断小数部分。

反编译 `NicoKaraMaker3.Models.ProjectData.ProjectDataModel.CreateLyricsLayout()` 可见，新建布局显式设置：

```text
LyricsInterval = SizeAndRatio.CreateAndSetAllBySizeAndReference(0, MovieInfo.Height)
RubyInterval = SizeAndRatio.CreateAndSetAllBySizeAndReference(0, MovieInfo.Height)
LyricsAndRubyInterval = SizeAndRatio.CreateAndSetAllBySizeAndReference(0, MovieInfo.Height)
```

因此本项目默认 `Style.ruby_gap_px = 0`，与 N3 的「歌詞とルビの間隔」默认值一致。视觉上仍会出现距离，是因为 N3 对齐的是 ruby 布局框下缘与正文 `DrawTop`，而不是两个墨水轮廓直接相贴。

## 影响布局的字体几何默认值

字体设置不属于 `LyricsLayoutModel`，但会直接改变字符步进和肉眼间距。N3 新建项目的标准歌词设置为：

| 项目 | 正文 | 假名 |
|---|---:|---:|
| 字号 `CharSize` | 100 | 45 |
| 第一描边 `EdgeSize` | 15 | 10 |
| 第二描边 `EdgeSize2` | 5 | 3 |

默认字体按安装情况依次选择 `HGP明朝E`、`游明朝`、`ＭＳ Ｐ明朝`。截图中的项目可以保存不同字体和字号，因此不能只靠新建项目默认值判断某张截图的实际设置。

## 内置布局预设

| 预设 | 垂直对齐 | 行间距 | 垂直边距 | 水平边距 | 各行水平对齐 |
|---|---|---:|---:|---:|---|
| 下寄せ2行 | 下 | 85 | 50 | 50 | 左、右 |
| 下寄せ3行 | 下 | 85 | 50 | 50 | 左、中、右 |
| 上寄せ2行 | 上 | 85 | 50 | 50 | 左、右 |
| コーラス | 下 | 85 | 505 | 50 | 中 |
| タイトル左上 | 上 | 15 | 50 | 50 | 左 |
| タイトル中央 | 中 | 30 | 50 | 200 | 中、右 |

所有预设的正文字间距、假名内部间距、正文—假名间距均为 0，默认不允许咬合，假名使用自动对齐。

## 正文字步进

N3 不使用“上一字墨水右缘到下一字墨水左缘保持固定距离”的模型。它先从 DirectWrite 字形轮廓取得墨水边界，再结合字体 design glyph metrics 计算每个字自己的布局宽度。

对普通字形，忽略缩放单位后可写成：

```text
布局宽度 = int(墨水轮廓宽度) × (左 side bearing + advance + 右 side bearing) / advance
左偏移   = int(墨水轮廓宽度) × 左 side bearing / advance
字步进   = 布局宽度 + 描边宽度 + LyricsInterval
```

N3 不会直接把字体轮廓放在字符框起点。它先把轮廓自身的 `bounds.Left` 归零，再按下面的位置绘制：

```text
轮廓墨水左缘 = 字符框起点 + 左偏移 + 第一描边宽度 / 2
```

第一描边完成后，其外缘从“字符框起点 + 左偏移”开始。这个重新定位步骤很重要：布局宽度与左偏移必须同时复刻，只复刻宽度会令窄字形在字符框内偏向一侧。

不允许咬合时，负的左右 side bearing 会先被钳制为 0；允许咬合时保留负值。因此 `LyricsInterval = 0` 也不等于墨水之间为 0 px，实际可见间隙仍随每个字形的轮廓和 side bearing 改变。

空格不走普通字形公式。N3 的全局 `SpaceWidth` 默认是字号的 20%；无轮廓的其他字符使用另一条兜底比例公式。

## 假名布局

假名先按自身布局宽度和 `RubyInterval` 排列，再相对于所标注的正文范围放置：

- `Center`：整组假名居中；
- `EqualSpace`：把剩余宽度均分到字符之间或两侧；
- `Auto`：正文或假名全部为字母数字时采用居中，否则采用均分。

N3 的 `EqualSpace` 不是 CSS 那种固定 slot 均分，而是按 `DrawWidth` 重新计算字符起点：

```text
ruby 自然宽度 = sum(每个假名字的 DrawWidth)

if 正文范围宽度 <= ruby 自然宽度:
    字间距 = (正文范围宽度 - ruby 自然宽度) / (假名字数 - 1)
else:
    字间距 = (正文范围宽度 - ruby 自然宽度) / (假名字数 + 1)

字间距 = max(字间距, RubyInterval)
ruby 起点 = 正文左缘 + (正文范围宽度 - (ruby 自然宽度 + 字间距 * (假名字数 - 1))) / 2
```

因此当 `ひかり` 这类假名比单个汉字更宽时，N3 会允许 ruby 起点落到正文左缘左侧，让整组 ruby 围绕正文范围居中溢出；不会把它硬钉在正文左缘。默认 `Auto` 对日文正文/假名会走这条 `EqualSpace` 路径，所以宽目标字上的 `しろ` 会被自然拉开。

如果假名组比正文范围更宽，N3 还会移动后续正文，消除假名碰撞。

垂直方向上，N3 令假名布局框的下缘位于 `正文 DrawTop - LyricsAndRubyInterval`。所以默认值 0 表示两个几何布局框相接，不表示正文墨水与假名墨水相接，也不保证肉眼可见间隙为 0。

## “めくるめく権謀”的间距现象

使用当前默认字体 `UD Digi Kyokasho N-B`、正文字号 100 px、描边 9 px，当前实现（仅复刻布局宽度、尚未应用 N3 左偏移）得到的可见墨水间隙约为：

| 相邻字 | 可见墨水间隙 |
|---|---:|
| め → く | 36.6 px |
| く → る | 27.3 px |
| る → め | 23.1 px |
| め → く | 36.6 px |
| く → 権 | 13.2 px |
| 権 → 謀 | 14.4 px |

`く` 的墨水明显窄，且左右 side bearing 不对称；布局宽度会被压缩到约 81 px，而宽汉字约为 107–108 px。但上表不能作为 N3 最终像素结果，因为 N3 还会应用 `CharGeometryLeftOffset` 重新放置墨水轮廓。当前实现漏掉该偏移，是局部视觉间距仍与 N3 不一致的原因。

以“寄り添って”为例，`り` 的左 side bearing 很大。当前实现让 QPainter 原样保留该留白，令 `り` 的墨水偏向字符框右侧；N3 会把左留白按轮廓宽度重新缩放后作为左偏移，使 `寄→り` 与 `り→添` 的描边外缘间距接近一致。
