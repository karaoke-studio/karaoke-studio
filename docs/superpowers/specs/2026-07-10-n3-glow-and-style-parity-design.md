# N3 发光浓度与样式对齐设计

## 目标

让字幕渲染模块严格应用 NicoKaraMaker3 10.74 项目中的三档发光浓度，并用回归测试锁定示例 `D:\カラオケ\songs\TACTIC\1.n3proj` 的假名样式、走字后蓝白配色和 7px 字间距。修复不得改写 N3 二重描边开关的真实语义。

## 已确认的示例数据

示例工程由 NicoKaraMaker3 10.74 保存，默认字体方案的相关值为：

- `DecorKind=2`：发光。
- `DecorSize=13`：发光尺寸 13px。
- `BlurLevel=1`：中档发光浓度。
- 主文字 `EdgeSize=2`，`EdgeSize2=5`，`UseEdge2=null`。
- 假名 `CharSize=45`，`EdgeSize=2`，`EdgeSize2=3`，`UseEdge2=null`。
- `UseEdge2` 按 N3 `Nkm3Common.UseEdge2()` 沿 fallback 链解析；链上没有布尔值时结果是 `false`。因此主文字和假名都不绘制二重描边，不能因 `EdgeSize2` 保存了数值就强制开启。
- 默认布局 `LyricsInterval=7`。
- 走字后四层颜色为文字 `#FFF1FB`、描边 `#4EAADE`、二重描边色 `#000000`、发光 `#4EAADE`。二重描边色虽保存，但因开关关闭而不绘制。

当前导入器已把假名 45px/2px/0px、蓝白走字后配色和 7px 字间距传入 `Style`；直接渲染和完整主窗口应用后这些值仍然保留。确定丢失的是 `BlurLevel`。另有一个独立缺口：角色方案应用到字符时，假名描边、装饰和发光覆盖字段未被复制到有效样式。

## 数据模型

使用与 N3 `BlurLevel` 直接对应的整数档位，取值只能是 `0` / `1` / `2`：

- `Style.glow_concentration_level: int = 0`：主文字发光浓度。
- `Style.ruby_glow_concentration_level: Optional[int] = None`：假名覆盖；`None` 时继承主文字。
- `SubtitleStyleScheme` 增加同名可选字段，保证自定义/角色方案可持久化。
- `TitleOverlay.glow_concentration_level: int = 0`：标题使用的 N3 字体方案也保留浓度。

读取旧 `.yurika` / `.krstyle.json` 时缺少字段即回退到 0，保持现有单层发光外观。非法值统一夹到 `0..2`。

## N3 发光算法

N3 `DrawOneLineDecorBlurMulti()` 的规则为：

```text
passes = BlurLevel + 1
for i in 0 .. passes-1:
    blur_radius = DecorSize - floor(i * DecorSize / passes)
    draw_same_decor_source_with_gaussian_blur(blur_radius)
```

因此 13px 在三档下的模糊半径分别为：

- 低（0）：`[13]`
- 中（1）：`[13, 7]`
- 高（2）：`[13, 9, 5]`

QPainter 实现中，发光源轮廓只构建一次，轮廓宽度始终基于原始 `DecorSize`，再对同一源图按上述半径分别模糊并叠加。这与 N3 每次重绘相同源轮廓的像素语义一致，且避免误把较小的模糊半径同时当成较细的源轮廓。

所有主文字、假名、走字前/后、动画缓存和标题的发光路径都必须传入对应浓度。缓存键必须包含浓度档位，避免切换档位后命中旧图层。

## 界面

在“颜色 / 字体”的装饰详细区增加“发光浓度”下拉框：

- 低 → 0
- 中 → 1
- 高 → 2

仅当当前图层为“装饰”且装饰类型为“发光”时显示。“编辑对象”为主文字时写主字段，为注音时写假名覆盖字段。“应用主文字配色”应同时清除假名浓度覆盖，使其重新继承主文字。

## N3 导入

- `DecorKind=Blur` 时将 `BlurLevel` 夹到 `0..2`，写入主文字与假名浓度；不再输出“暂不支持”警告。
- 自定义字体方案同样保留该档位。
- 标题引用的字体方案也写入 `TitleOverlay`。
- `UseEdge2` 继续严格按 N3 fallback 语义解析；本次不改动示例工程的 0px 二重描边结果。
- `LyricsInterval=7` 继续写入 `Style.letter_spacing_px`，并用回归测试确认经 `style_to_dict()` / `style_from_dict()` 和主窗口应用后仍为 7。
- 走字后四层颜色按 `BrushInfos[0:4]` 导入，用回归测试锁定示例的蓝白数值。

## 角色方案中的假名覆盖

`engine.painter._SUBTITLE_SCHEME_STYLE_FIELDS` 必须补齐以下已存在于 `SubtitleStyleScheme` 但当前未应用的字段：

- `ruby_stroke_width_px`
- `ruby_stroke2_width_px`
- `ruby_decoration_kind`
- `ruby_glow_radius_px`
- `ruby_glow_before_radius_px`
- `ruby_glow_after_radius_px`
- `ruby_glow_concentration_level`
- `ruby_shadow_offset_x`
- `ruby_shadow_offset_y`

主文字的 `glow_concentration_level` 也必须加入方案字段。这一修复保证 N3 字符 `FontIndex` 切换到自定义方案时，假名与该方案的描边和发光一起切换。

## 测试

使用 TDD 按下列顺序增加回归：

1. 模型持久化：主文字、假名、自定义方案和标题的浓度往返不丢失，非法值夹到有效范围。
2. 导入器：`BlurLevel=0/1/2` 正确映射，警告消失；示例工程保持二重描边关闭、假名 45px/2px/0px、走字后蓝白四层颜色和 7px 字间距。
3. 算法：13px 的三档半径序列分别为 `[13]` / `[13, 7]` / `[13, 9, 5]`。
4. 像素行为：相同颜色与半径下，三档输出图像两两不同，且更高浓度增加发光累积 alpha。
5. 缓存：浓度变化不得命中上一档的发光缓存。
6. 界面：三档下拉框的显隐、主文字/假名字段写入和“应用主文字配色”继承行为。
7. 角色方案：`_style_for_role()` 保留假名描边、装饰、半径和浓度覆盖。

定向测试通过后运行所有 `subtitle_render` 测试，再运行全量 `tests\`。另外渲染示例歌词行的走字前/中/后帧做人工视觉检查。

## 本次不扩展的 N3 能力

> 2026-07-11 校准：本节是发光专项当时的范围边界，不再作为当前待办清单。
> 逐行不同动作现已实现；其余能力的最新产品决策和顺序见
> [`../../N3项目导入兼容性与实施计划.md`](../../N3项目导入兼容性与实施计划.md)。

以下缺口会在交付时明确列出，但不混入本次样式对齐：

- 输出 30fps：示例工程为 30fps，当前模块只允许 60/120fps，导入时调整为 60fps。
- 每布局独立字符域：N3 可让每个布局分别持有 `LyricsInterval` / `RubyInterval` / `RubyAlignment` / `LyricsAndRubyInterval` / `AllowBiting`，当前模块对这些字段使用全局值。示例主歌词全部引用默认布局，因此其 7px 字间距本次不受该缺口影响。
- N3 图片/图片序列/纯色背景、非 MP4 输出、完整 MP4 编码器预设和未知字幕动作。
- 独立假名字体族、假名/英数的全部 N3 fallback 可编辑能力；示例假名字体继承主文字，不触发该缺口。
