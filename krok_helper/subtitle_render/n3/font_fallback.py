"""Pure-data NicoKaraMaker3 font-slot fallback resolution.

N3 stores six ``FontFaceInfoModel`` entries, but Lin-K Lyrics deliberately
ignores the two kana entries (1/4).  Kana uses the Japanese settings from slot
0 for lyrics and slot 3 for ruby.  This module resolves the four effective
slots without importing Qt so both ``.n3proj`` snapshots and ``.tpl`` templates
share exactly the same fallback rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


SizeResolver = Callable[[object], int]
FaceWeightResolver = Callable[[str, str], int]

N3_DEFAULT_FONT_SIZE = 100
N3_DEFAULT_EDGE_SIZE = 5
N3_DEFAULT_EDGE2_SIZE = 5
N3_DEFAULT_FONT_WEIGHT = 700

# Slots 1/4 are intentionally absent: kana always follows the Japanese slot.
_FALLBACK_INDEX: dict[int, int] = {2: 0, 3: 0, 5: 3}
_EFFECTIVE_SLOT_INDICES = (0, 2, 3, 5)


@dataclass(frozen=True)
class ResolvedN3FontSlot:
    """Materialized visual values for one supported N3 font slot."""

    family: str
    face_name: str
    weight: int
    char_size: int
    edge_size: int
    use_edge2: bool
    edge2_size: int


def _font_info(font_infos: Sequence[object], index: int) -> Mapping[str, object]:
    if not 0 <= index < len(font_infos):
        return {}
    value = font_infos[index]
    return value if isinstance(value, Mapping) else {}


def _slot_chain(font_infos: Sequence[object], index: int) -> list[Mapping[str, object]]:
    chain: list[Mapping[str, object]] = []
    seen: set[int] = set()
    current: int | None = index
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(_font_info(font_infos, current))
        current = _FALLBACK_INDEX.get(current)
    return chain


def _resolve_family_and_face(
    chain: Sequence[Mapping[str, object]],
    default_family: str,
    face_weight_resolver: FaceWeightResolver,
) -> tuple[str, str, int]:
    """Resolve family and face as one indivisible N3 fallback value."""

    for info in chain:
        family = str(info.get("FontName") or "").strip()
        if not family:
            continue
        face_name = str(info.get("FontFaceName") or "").strip()
        # An empty/unknown face on a selected family is Normal.  It must not
        # independently inherit the fallback slot's face.
        return family, face_name, int(face_weight_resolver(family, face_name))
    return default_family, "", N3_DEFAULT_FONT_WEIGHT


def _resolve_positive_size(
    chain: Sequence[Mapping[str, object]],
    key: str,
    size_resolver: SizeResolver,
    default: int,
) -> int:
    for info in chain:
        value = int(size_resolver(info.get(key)))
        if value > 0:
            return value
    return default


def _resolve_use_edge2(chain: Sequence[Mapping[str, object]]) -> bool:
    for info in chain:
        value = info.get("UseEdge2")
        if isinstance(value, bool):
            return value
    return False


def resolve_n3_font_slots(
    font_infos: Sequence[object],
    *,
    size_resolver: SizeResolver,
    default_family: str,
    face_weight_resolver: FaceWeightResolver,
) -> dict[int, ResolvedN3FontSlot]:
    """Resolve and materialize N3 slots 0/2/3/5.

    ``size_resolver`` defines the entry semantics: a ``.n3proj`` caller reads
    the stored ``Size`` directly, while a ``.tpl`` caller can apply its target
    height to ``Ratio`` before the independent numeric fallback runs.
    """

    resolved: dict[int, ResolvedN3FontSlot] = {}
    for index in _EFFECTIVE_SLOT_INDICES:
        chain = _slot_chain(font_infos, index)
        family, face_name, weight = _resolve_family_and_face(
            chain, default_family, face_weight_resolver
        )
        resolved[index] = ResolvedN3FontSlot(
            family=family,
            face_name=face_name,
            weight=weight,
            char_size=_resolve_positive_size(
                chain, "CharSize", size_resolver, N3_DEFAULT_FONT_SIZE
            ),
            edge_size=_resolve_positive_size(
                chain, "EdgeSize", size_resolver, N3_DEFAULT_EDGE_SIZE
            ),
            use_edge2=_resolve_use_edge2(chain),
            edge2_size=_resolve_positive_size(
                chain, "EdgeSize2", size_resolver, N3_DEFAULT_EDGE2_SIZE
            ),
        )
    return resolved
