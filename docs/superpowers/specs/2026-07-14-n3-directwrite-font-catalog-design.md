# N3 DirectWrite 字体目录对齐设计

## 目标

Windows 上字幕渲染模块的四个字体下拉框使用与 NicoKaraMaker3
10.74.80.0 相同的实际字体集合、显示名称和顺序。保留工作台现有的中文继承项，
不要求复刻 N3 的空白继承项。

加载旧设置或项目时，把仍可由 DirectWrite 识别的英文/本地化别名规范化为 N3
菜单采用的标准名称；真正不存在的名称不再被追加到字体菜单，并按字段语义回退。

## N3 事实来源

以本机安装包
`D:\カラオケ\NicokaraMaker3_10.74.80.0_x64\NicokaraMaker3_10.74.80.0_x64`
中的 `NicoKaraMaker3.dll` 为事实来源。反编译与本机探针确认 N3 的算法如下：

1. 调用 DirectWrite `GetSystemFontCollection(false)`。
2. 遍历每个 `IDWriteFontFamily`。
3. 字体族名称优先取 `ja-jp`；不存在时取本地化名称列表第 0 项。
4. 仅保留至少含一个 `FontStyle.Normal` 字体面的字体族。
5. 有 `ja-jp` 名称的字体族排在前面；两组内部使用 `ja-JP` 当前区域的
   `string.Compare` 语义排序。
6. N3 在列表开头插入空字符串作为继承项；工作台改为保留语义更明确的中文继承项。
7. 项目中保存但不属于菜单的名称不会反向追加到菜单。

本机验证得到 N3 规则下 240 个非空字体族；Qt `QFontDatabase.families()` 返回
417 项，多出的 178 项主要是字体面、字重和英文别名。

## 方案选择

### 采用：直接调用 Windows DirectWrite

新增独立的 Windows 字体目录模块，通过 Python `ctypes` 调用系统 `dwrite.dll`。
它负责枚举 N3 字体、生成标准名称和别名映射。`dwrite.dll` 是 Windows 系统组件，
PyInstaller 无需额外收集二进制依赖。

### 不采用：过滤 `QFontDatabase`

Qt 已经把部分字体面和本地化别名展开成独立 family。仅凭名称规则无法可靠还原
DirectWrite 的字体族边界、`ja-jp` 标准名称和 Normal face 条件。

### 不采用：读取注册表或直接扫描字体文件

注册表和字体目录不能完整覆盖按用户安装、字体集合、本地化名称及 DirectWrite 的
可用性判断；自行解析字体文件也会复制系统已经提供的逻辑。

## 架构与数据流

新增 `krok_helper/subtitle_render/n3_font_catalog.py`，对外提供不依赖 UI 的接口：

- `n3_font_families() -> tuple[str, ...]`：返回不含继承项的 N3 标准字体名称；
- `canonicalize_n3_font_family(name: str) -> str | None`：把任一本地化别名映射到
  N3 标准名称，真正不存在或不满足 Normal face 条件时返回 `None`；
- `normalize_style_font_families(style: Style) -> tuple[Style, bool]`：规范化全局、
  角色、标题、歌手覆盖及 N3 继承方案中的字体字段，并报告是否发生变化。

Windows 实现缓存一次 DirectWrite 快照。macOS 没有 DirectWrite，继续使用
`QFontDatabase.families()`，只做精确名称匹配，不尝试声称与 N3 列表一致。

字体面板初始化时：

1. 先加入当前字段需要的中文继承项；
2. 再加入 `n3_font_families()` 返回的字体；
3. 设置当前字体时先规范化名称；
4. 未命中的保存值不得调用 `addItem()` 追加到菜单。

## 旧数据迁移

所有样式加载入口共用同一规范化函数：应用级 `settings.json`、`.yurika` 项目、
N3 项目导入和字体预设。

- 可解析别名：改写成 N3 标准名称。例如
  `UD Digi Kyokasho N-B` 改为 `UD デジタル 教科書体 N-B`。
- 可继承的可选字体字段：真正缺失时清为 `None`，回到本方案定义的继承行为。
- 必填的全局主字体或独立方案根字体：真正缺失时改为当前系统可用的 N3 默认字体，
  优先顺序沿用项目现有的 `HGP明朝E → 游明朝 → ＭＳ Ｐ明朝` 规则。
- 应用级设置在启动迁移后立即保存规范化结果，保证长期残留名称被清除。
- 项目文件只在内存中规范化；用户明确保存项目时才写回，打开项目不能静默修改源文件。

规范化只修改字体族名称，不改变字号、字重、描边、颜色、布局或角色引用。

## DirectWrite 边界与错误处理

COM 指针由小型内部包装器负责释放，任何已取得的 factory、collection、family、font
和 localized strings 都必须在异常路径释放。

如果 Windows DirectWrite 初始化或枚举失败：

- 记录诊断日志；
- 本次会话回退到 `QFontDatabase.families()`，保证界面仍可使用；
- 不执行破坏性的旧名称清理，避免在字体目录不可用时误判字体缺失。

排序使用 Windows `CompareStringEx("ja-JP", ...)` 的比较结果，复刻 N3 在 Windows
上的日文区域排序，不依赖 Python 进程当前区域设置。

## 验证与测试

1. 纯数据测试锁定：日文名称优先、无日文名称回退第 0 项、Normal face 过滤、
   日文组优先和别名规范化。
2. UI 测试锁定：四个字体框保留中文继承项；未知名称不追加；别名选中标准名称。
3. 设置迁移测试锁定：别名写回标准名称、缺失可选字段清空、缺失根字体安全回退，
   DirectWrite 失败时不清理。
4. Windows 集成探针把实现结果与按 N3 反编译算法独立枚举的结果逐项比较；测试不得
   硬编码本机的 240 项，因为 CI runner 的字体安装状态可能不同。
5. 运行对应字幕前端、模型、设置及 N3 导入测试，再执行 Qt offscreen 冒烟。

## 验收标准

- 同一台 Windows、同一字体安装状态下，工作台实际字体项与 N3 10.74.80.0
  逐项相同且顺序相同；允许中文继承项与 N3 空白项不同。
- 重启后 `UD Digi Kyokasho N-B` 不再作为独立菜单项出现，保存值变为
  `UD デジタル 教科書体 N-B`。
- 项目残留的未知名称不会污染菜单。
- macOS 行为不退化，Windows DirectWrite 故障不会触发误清理。
