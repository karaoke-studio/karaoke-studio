"""波形对齐页与宿主之间的边界。

``AlignmentPageMixin`` 目前还混在 ``KrokHelperQtApp`` 上，``self`` 是同一个
对象 —— 也就是说它随时可以顺手多摸一个宿主成员，而没有任何东西会拦。这条
测试就是那道拦：把「页面还依赖宿主的哪些成员」写死成一份清单，多一个就红。

清单只该变短。要变长必须是有意为之：改这里之前先想清楚，那个成员是该跟着
页面搬过来，还是该走 :mod:`krok_helper.workflow_host` 那样的显式接口。
"""

from __future__ import annotations

import ast
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "alignment" / "page.py"

#: 页面调用的宿主服务 —— 变成独立对象时，这些要么跟着搬，要么进宿主接口。
HOST_SERVICES = {
    "settings",  # 配置读写
    "_resolve_ffmpeg_dir",  # ffmpeg 位置
    "_track_background_task",  # 后台任务登记，关窗时统一收尾
    "_build_media_info",  # 素材信息文案（Hi-Res 页也在用）
    "_set_panel_enabled",  # 忙碌时整块禁用
    "_notify_handoff",  # 转交产物的右下角提示
    "set_on_vocal_path",  # 把原唱交给第 6 步
    "_open_settings_window",  # 打开全局设置的对齐分页
    "active_module",  # 快捷键是否该响应
    "_focused_widget_is_text_input",  # 同上：焦点在输入框里就别抢按键
    "_loading_settings_into_ui",  # 灌设置期间抑制回写
    "preview_timer",  # 只服务本页，改对象时应当跟着搬
    "hide",  # QWidget 自己的
}

#: 只在 ``hasattr`` 保护下出现、全仓从未赋值的旧控件名 —— 早期版本的遗留，
#: 现在恒为假分支。留着不动是因为删除等于改行为路径；单独清理更安全。
DEAD_WIDGET_NAMES = {
    "align_step_small_button",
    "align_step_large_button",
    "align_target_video_card",
    "align_target_audio_card",
    "subtitle_accent_bar",
    "original_accent_bar",
    "subtitle_adjust_badge",
    "original_adjust_badge",
}


def _foreign_members() -> set[str]:
    """页面读了、但既不是自己的方法也不是自己赋值过的成员。"""
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own |= {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)
    return read - own - assigned


def test_the_page_only_reaches_for_the_declared_host_surface() -> None:
    unexpected = _foreign_members() - HOST_SERVICES - DEAD_WIDGET_NAMES

    assert not unexpected, (
        "对齐页多摸了宿主成员：" + "、".join(sorted(unexpected)) + "。"
        "先判断它该跟着页面搬、还是该走显式宿主接口，再决定要不要加进清单。"
    )


def test_the_declared_surface_has_not_gone_stale() -> None:
    """清单里列了、页面其实已经不用的成员，要及时删掉，否则清单会失真。"""
    stale = (HOST_SERVICES | DEAD_WIDGET_NAMES) - _foreign_members()

    assert not stale, "清单里这些已经不再被引用，可以删了：" + "、".join(sorted(stale))


def test_the_page_does_not_import_the_shell() -> None:
    """页面反向 import ``gui_qt`` 会形成循环依赖，也说明边界破了。"""
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)
