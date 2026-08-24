"""N3 font-slot fallback rules shared by project and template imports."""

from krok_helper.subtitle_render.n3.font_fallback import resolve_n3_font_slots


def _size(value: int, ratio: float = 0.0) -> dict[str, float | int]:
    return {"Size": value, "Reference": 1080, "Ratio": ratio}


def _project_size(value: object) -> int:
    return int(value.get("Size", 0)) if isinstance(value, dict) else 0


def _slot(
    family: str = "",
    face: str = "",
    char_size: int = 0,
    edge_size: int = 0,
    use_edge2: bool | None = None,
    edge2_size: int = 0,
) -> dict[str, object]:
    return {
        "FontName": family,
        "FontFaceName": face,
        "CharSize": _size(char_size),
        "EdgeSize": _size(edge_size),
        "UseEdge2": use_edge2,
        "EdgeSize2": _size(edge2_size),
    }


def _weight(_family: str, face: str) -> int:
    return {"Regular": 400, "Bold": 700, "Black": 900}.get(face, 400)


def test_family_and_face_fallback_as_a_pair():
    infos = [
        _slot("Japanese", "Bold", 100, 15, False, 5),
        _slot(),
        _slot("Latin", "", 0, 0, None, 0),
    ]

    latin = resolve_n3_font_slots(
        infos,
        size_resolver=_project_size,
        default_family="Default",
        face_weight_resolver=_weight,
    )[2]

    assert latin.family == "Latin"
    assert latin.face_name == ""
    assert latin.weight == 400
    # Numeric fields have their own chains even though the family stopped at 2.
    assert (latin.char_size, latin.edge_size, latin.edge2_size) == (100, 15, 5)
    assert latin.use_edge2 is False


def test_each_numeric_field_and_use_edge2_falls_back_independently():
    infos = [
        _slot("Japanese", "Bold", 100, 15, True, 5),
        _slot(),
        _slot(),
        _slot("Ruby", "Regular", 30, 0, False, 2),
        _slot(),
        _slot("Ruby Latin", "Black", 0, 7, None, 0),
    ]

    ruby_latin = resolve_n3_font_slots(
        infos,
        size_resolver=_project_size,
        default_family="Default",
        face_weight_resolver=_weight,
    )[5]

    assert (ruby_latin.family, ruby_latin.weight) == ("Ruby Latin", 900)
    assert ruby_latin.char_size == 30
    assert ruby_latin.edge_size == 7
    assert ruby_latin.use_edge2 is False
    assert ruby_latin.edge2_size == 2


def test_empty_chains_use_n3_defaults_and_ignore_kana_slots():
    infos = [
        _slot(),
        _slot("Ignored lyric kana", "Black", 222, 33, True, 11),
        _slot(),
        _slot(),
        _slot("Ignored ruby kana", "Black", 222, 33, True, 11),
        _slot(),
    ]

    slots = resolve_n3_font_slots(
        infos,
        size_resolver=_project_size,
        default_family="N3 Default",
        face_weight_resolver=_weight,
    )

    assert set(slots) == {0, 2, 3, 5}
    for slot in slots.values():
        assert (slot.family, slot.face_name, slot.weight) == ("N3 Default", "", 700)
        assert (slot.char_size, slot.edge_size, slot.edge2_size) == (100, 5, 5)
        assert slot.use_edge2 is False


def test_size_resolver_allows_template_ratio_before_fallback():
    infos = [_slot("Japanese", "Regular"), _slot(), _slot()]
    infos[0]["CharSize"] = _size(133, ratio=100 / 1080)

    def template_size(value: object) -> int:
        if not isinstance(value, dict):
            return 0
        ratio = float(value.get("Ratio", 0))
        return int(1080 * ratio) if ratio else int(value.get("Size", 0))

    latin = resolve_n3_font_slots(
        infos,
        size_resolver=template_size,
        default_family="Default",
        face_weight_resolver=_weight,
    )[2]

    assert latin.char_size == 100
