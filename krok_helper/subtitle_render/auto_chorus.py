"""按括号自动识别和声段，给里面的字符分配角色方案。

对标 NicoKaraMaker3 的 ``PartFontSelector``（「パート別にフォントを設定」）里的
``AutoChorus``。逆向自 ``NicoKaraMaker3.dll`` 的 ``AnalyzeAutoChorus``，四条边界
行为都照抄：

* 起止是**字符集合**而不是字符串 —— 默认起始 ``（(``、结束 ``）)``，全角半角都认；
* **括号自己也算在和声段里**，一起变色；
* 一行里**没闭合**就 ``break``，本行后面不再找（不是跳过继续）；
* 收口后从结束括号的下一个字符接着扫，所以**一行可以有多段**；不支持嵌套，
  遇到的第一个结束字符就收口。

只负责算出"哪几段是和声"，不碰任何 UI、也不决定要不要覆盖已有角色 —— 那是调用
方的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "DEFAULT_CHORUS_BEGIN_CHARS",
    "DEFAULT_CHORUS_END_CHARS",
    "DEFAULT_CHORUS_ROLE_NAMES",
    "ChorusSpan",
    "apply_chorus_roles",
    "detect_chorus_spans",
    "pick_chorus_role",
]

#: 默认起止字符（与 N3 的 ``AutoChorusBeginChars`` / ``AutoChorusEndChars`` 一致）。
DEFAULT_CHORUS_BEGIN_CHARS = "（("
DEFAULT_CHORUS_END_CHARS = "）)"

#: 挑默认角色时优先认这些名字。N3 找的是名字里含「コーラス」的配色方案，
#: 我们的项目里中文用户更可能叫「和声」。
DEFAULT_CHORUS_ROLE_NAMES = ("和声", "コーラス", "chorus")


@dataclass(frozen=True)
class ChorusSpan:
    """一段和声在行内的字符下标区间，**含**首尾两个括号。"""

    start: int
    end: int
    """闭区间的右端 —— 结束括号自己的下标。"""

    def indices(self) -> range:
        return range(self.start, self.end + 1)


def detect_chorus_spans(
    texts: Sequence[str],
    *,
    begin_chars: str = DEFAULT_CHORUS_BEGIN_CHARS,
    end_chars: str = DEFAULT_CHORUS_END_CHARS,
) -> list[ChorusSpan]:
    """从一行的字符文本序列里找出所有和声段。

    ``texts`` 是逐字符的文本（一般就是 ``[c.text for c in line.chars]``）。多字符
    的元素（共享时间块里被合并的那种）不参与起止判断 —— 括号总是单独一个字符。
    """
    if not begin_chars or not end_chars:
        return []
    spans: list[ChorusSpan] = []
    index = 0
    total = len(texts)
    while index < total:
        if texts[index] not in begin_chars:
            index += 1
            continue
        closing = index + 1
        while closing < total and texts[closing] not in end_chars:
            closing += 1
        if closing >= total:
            # 没闭合：N3 在这里 break，本行剩下的部分整个放弃。
            break
        spans.append(ChorusSpan(index, closing))
        index = closing + 1
    return spans


def pick_chorus_role(available_roles: Iterable[str]) -> str:
    """在已有角色里挑一个当和声角色；都不像就返回 ``"和声"``（由调用方新建）。"""
    names = [str(name).strip() for name in available_roles if str(name).strip()]
    for keyword in DEFAULT_CHORUS_ROLE_NAMES:
        for name in names:
            if keyword in name:
                return name
    return DEFAULT_CHORUS_ROLE_NAMES[0]


def apply_chorus_roles(
    texts: Sequence[str],
    current_labels: Sequence[str | None],
    role: str,
    *,
    begin_chars: str = DEFAULT_CHORUS_BEGIN_CHARS,
    end_chars: str = DEFAULT_CHORUS_END_CHARS,
    overwrite: bool = False,
) -> list[str | None]:
    """算出一行的新角色标签；没有任何改动时返回与输入等值的列表。

    ``overwrite=False``（默认）时**只填还没有角色的字符**。我们的角色可能来自
    打轴模块里一个个点出来的歌手分配，无脑覆盖等于把那份工作抹掉。
    """
    labels = list(current_labels)
    for span in detect_chorus_spans(texts, begin_chars=begin_chars, end_chars=end_chars):
        for index in span.indices():
            if index >= len(labels):
                break
            if not overwrite and labels[index]:
                continue
            labels[index] = role
    return labels
