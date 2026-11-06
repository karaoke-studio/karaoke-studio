from __future__ import annotations

import krok_helper  # noqa: F401 - installs bundled SUG src path
from krok_helper.lyrics import (
    DEFAULT_LYRICS_PROVIDER_IDS,
    LYRICS_PREVIEW_LINE,
    UTATEN_RUBY_MARKER,
    LyricsSearchCandidate,
    UtatenLyricsProvider,
    _parse_utaten_lyrics_page,
    _parse_utaten_search_results,
    build_default_providers,
    build_lyrics_preview,
)
from krok_helper.sug_compat import apply_sug_compat_patches
from strange_uta_game.backend.domain.models import get_ruby_pause_char, pause_char_variants
from strange_uta_game.frontend.editor.timing.lyric_loader import detect_lyric_format, parse_lyric_content


def _ruby_text_without_pause(text: str) -> str:
    pause_chars = pause_char_variants(get_ruby_pause_char())
    return "".join(ch for ch in text if ch not in pause_chars)


def _character_ruby_text_without_pause(character) -> str:
    ruby = getattr(character, "ruby", None)
    if ruby is None:
        return ""
    return _ruby_text_without_pause("".join(part.text for part in ruby.parts))


class _FakeSettings:
    def __init__(self, entries: list[dict]):
        self._entries = entries

    def get_all(self) -> dict:
        return {"auto_check": {}}

    def get(self, _key: str, default=None):
        return default

    def load_effective_dictionary(self) -> list[dict]:
        return self._entries


def test_parse_utaten_search_results() -> None:
    html = """
    <table class="searchResult artistLyricList">
      <tr><th>header</th></tr>
      <tr>
        <td><p class="searchResult__title"><a href="/lyric/abc123/">春日影</a></p></td>
        <td class="searchResult__artist"><p><a href="/artist/1/">MyGO!!!!!</a></p></td>
        <td class="lyricList__beginning"><a href="/lyric/abc123/">悴んだ心</a></td>
      </tr>
    </table>
    """

    results = _parse_utaten_search_results(html, base_url="https://utaten.com")

    assert results == [
        {
            "title": "春日影",
            "artist": "MyGO!!!!!",
            "snippet": "悴んだ心",
            "url": "https://utaten.com/lyric/abc123/",
        }
    ]


def test_parse_utaten_lyrics_page_to_marked_lrc() -> None:
    html = """
    <div class="lyricBody">
      <div class="medium">
        <div class="hiragana">
          <span class="ruby"><span class="rb">国道</span><span class="rt">こくどう</span></span>を行く<br />
          <span class="ruby"><span class="rb">右手</span><span class="rt">みぎて</span></span>に曲がる
        </div>
      </div>
    </div>
    """

    plain, marked = _parse_utaten_lyrics_page(html, title="Ruby", artist="GREAT3")

    assert plain == "国道を行く\n右手に曲がる"
    assert marked.splitlines() == [
        UTATEN_RUBY_MARKER,
        "[ti:Ruby]",
        "[ar:GREAT3]",
        "{国道||こくどう}を行く",
        "{右手||みぎて}に曲がる",
    ]


def test_default_providers_include_utaten() -> None:
    assert "utaten" in {provider.provider_id for provider in build_default_providers()}


def test_aggregate_default_excludes_utaten() -> None:
    # UtaTen 走带注音的专用通道，不混进聚合搜索（聚合优先返回 QQ/酷狗/网易等通用来源的同步歌词）。
    assert "utaten" not in DEFAULT_LYRICS_PROVIDER_IDS


def test_utaten_search_treats_whole_keyword_as_title(monkeypatch) -> None:
    provider = UtatenLyricsProvider()
    calls: list[str] = []

    def fake_search_once(*, title: str, sort="popular_sort_asc") -> list[dict[str, str]]:
        calls.append(title)
        if title == "Ruby GREAT3":
            return [{"title": "Ruby GREAT3", "artist": "GREAT3", "snippet": "",
                     "url": "https://utaten.com/lyric/1/"}]
        return []

    monkeypatch.setattr(provider, "_search_once", fake_search_once)

    results = provider.search("Ruby GREAT3", limit=10)

    assert [(item.title, item.artist) for item in results] == [("Ruby GREAT3", "GREAT3")]
    assert calls == ["Ruby GREAT3"]


def test_sug_distributes_utaten_ruby_per_kanji_via_dict() -> None:
    # 字典命中（一字一音干净对应）：国道/こくどう → 国=こく, 道=どう。
    # 这种情况两字应独立成词（linked_to_next=False）—— SUG 的 F3 拆词
    # 默认行为是把"一字一读"的块拆开，让每字单独打轴。
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:Ruby]",
            "{国道||こくどう}を行く",
        ]
    )

    assert detect_lyric_format(content) == "utaten"
    sentences, is_nicokara, _new_singers, meta = parse_lyric_content(content, "singer-1")

    assert is_nicokara is False
    assert meta["format"] == "utaten"
    assert len(sentences) == 1
    chars = sentences[0].characters
    assert "".join(ch.char for ch in chars) == "国道を行く"
    assert chars[0].ruby is not None and chars[0].ruby.text == "こく"
    assert chars[1].ruby is not None and chars[1].ruby.text == "どう"
    assert chars[0].linked_to_next is False
    assert chars[1].linked_to_next is False


def test_sug_distributes_utaten_ruby_per_kanji_ateji_even_split() -> None:
    # 当て字场景：新時代 读作 はじまり —— 三字哪个都不读 はじ/ま/り，
    # 字典查不到组合 → 均分 2+1+1，并保持连词块完整性（单独读"ま"或
    # "り"无语义），三字必须 linked_to_next。
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:新時代]",
            "{新時代||はじまり}を",
        ]
    )

    sentences, _is_nicokara, _new_singers, meta = parse_lyric_content(content, "singer-1")

    assert meta["format"] == "utaten"
    assert len(sentences) == 1
    chars = sentences[0].characters
    assert "".join(ch.char for ch in chars) == "新時代を"
    assert chars[0].ruby is not None and chars[0].ruby.text == "はじ"
    assert chars[1].ruby is not None and chars[1].ruby.text == "ま"
    assert chars[2].ruby is not None and _character_ruby_text_without_pause(chars[2]) == "り"
    assert chars[0].linked_to_next is True
    assert chars[1].linked_to_next is True
    assert chars[2].linked_to_next is False


def test_sug_distributes_utaten_ruby_per_kanji_sekai_dict_split() -> None:
    # 字典命中且非均分巧合：世界/せかい —— 世=せ(セ 音读), 界=かい(カイ 音读)。
    # 1+2 分布是字典查到的真实结果（不是均分），两字独立成词。
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:World]",
            "{世界||せかい}を変える",
        ]
    )

    sentences, *_ = parse_lyric_content(content, "singer-1")
    chars = sentences[0].characters
    assert chars[0].ruby is not None and chars[0].ruby.text == "せ"
    assert chars[1].ruby is not None and chars[1].ruby.text == "かい"
    assert chars[0].linked_to_next is False
    assert chars[1].linked_to_next is False


def test_sug_aligns_utaten_ruby_with_user_dictionary_kotoba() -> None:
    # Utaten 的读音仍用「ことば」，但是否拆词/连词要复用正常 SUG 导入时的用户词典。
    settings = _FakeSettings([{"enabled": True, "word": "言葉", "reading": "こと,ば"}])
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:Frozen]",
            "{言葉||ことば}にできず凍えたままで",
        ]
    )

    sentences, *_ = parse_lyric_content(content, "singer-1", setting_iface=settings)
    chars = sentences[0].characters
    assert chars[0].ruby is not None and chars[0].ruby.text == "こと"
    assert chars[1].ruby is not None and chars[1].ruby.text == "ば"
    assert chars[0].check_count == 2
    assert chars[1].check_count == 1
    assert chars[0].linked_to_next is False
    assert chars[1].linked_to_next is False


def test_sug_aligns_utaten_ruby_with_user_dictionary_koburi() -> None:
    settings = _FakeSettings([{"enabled": True, "word": "小降", "reading": "こ,ぶ"}])
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:Rain]",
            "きみの町じゃもう雨は{小降||こぶ}りになる",
        ]
    )

    sentences, *_ = parse_lyric_content(content, "singer-1", setting_iface=settings)
    chars = sentences[0].characters
    text = "".join(ch.char for ch in chars)
    start = text.index("小")
    assert chars[start].ruby is not None and chars[start].ruby.text == "こ"
    assert chars[start + 1].ruby is not None and chars[start + 1].ruby.text == "ぶ"
    assert chars[start].check_count == 1
    assert chars[start + 1].check_count == 1
    assert chars[start].linked_to_next is False
    assert chars[start + 1].linked_to_next is False


def test_build_lyrics_preview_preserves_utaten_marker_with_strip_intro() -> None:
    # 默认勾选「省略歌曲介绍」会让 _strip_leading_intro_lines 把开头的
    # `[tool:utaten-ruby]` + `[ti:]` + `[ar:]` 当 intro credit 删掉，
    # 进而打断 SUG 的 utaten 识别。回归用例：确认 marker 不再被吃掉。
    marked_lrc = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:Ruby]",
            "[ar:GREAT3]",
            "{国道||こくどう}を行く",
            "{右手||みぎて}に曲がる",
        ]
    )
    candidate = LyricsSearchCandidate(
        provider_id="utaten",
        provider_name="UtaTen",
        track_id="abc123",
        title="Ruby",
        artist="GREAT3",
        album="",
        duration_seconds=None,
        plain_lyrics=marked_lrc,
        lyrics_loaded=True,
    )

    preview = build_lyrics_preview(candidate, LYRICS_PREVIEW_LINE, strip_intro_lines=True)

    assert preview.text.splitlines()[0] == UTATEN_RUBY_MARKER
    assert detect_lyric_format(preview.text) == "utaten"


def test_embedded_sug_utaten_import_preserves_katakana_ruby() -> None:
    apply_sug_compat_patches()
    content = "\n".join(
        [
            UTATEN_RUBY_MARKER,
            "[ti:RES\u221eNALIST]",
            "{\u5149||\u3072\u304b\u308a}\u7206\u305c\u308b{\u93ae\u9b42\u6b4c||\u30eb\u30af\u30a4\u30a8\u30e0}",
        ]
    )

    sentences, _is_nicokara, _new_singers, meta = parse_lyric_content(content, "singer-1")

    assert meta["format"] == "utaten"
    chars = sentences[0].characters
    assert "".join(ch.char for ch in chars) == "\u5149\u7206\u305c\u308b\u93ae\u9b42\u6b4c"
    start = 4
    imported_reading = _ruby_text_without_pause("".join(
        "".join(part.text for part in chars[index].ruby.parts)
        for index in range(start, start + 3)
        if chars[index].ruby is not None
    ))
    assert imported_reading == "\u30eb\u30af\u30a4\u30a8\u30e0"


def test_utaten_sends_one_title_request_with_whole_title(monkeypatch) -> None:
    from urllib.parse import urlencode
    import krok_helper.lyrics as lyrics

    calls: list[str] = []
    def load(request):
        calls.append(request.full_url)
        return """<table class="searchResult"><tr>
          <td><p class="searchResult__title"><a href="/lyric/1/">君の名は 主題歌</a></p></td>
          <td class="searchResult__artist"><p><a href="/artist/1/">RADWIMPS</a></p></td>
        </tr></table>"""

    monkeypatch.setattr(lyrics, "_load_text_from_request", load)
    results = UtatenLyricsProvider()._search_once(title="君の名は 主題歌")
    assert results[0]["url"] == "https://utaten.com/lyric/1/"
    assert len(calls) == 1
    assert urlencode({"title": "君の名は 主題歌"}) in calls[0]
    assert "artist_name=&" in calls[0]
    assert "page=1" in calls[0]
    assert "layout_search_text" not in calls[0]


def test_utaten_empty_response_is_not_no_results(monkeypatch) -> None:
    import pytest
    import krok_helper.lyrics as lyrics
    monkeypatch.setattr(lyrics, "_load_text_from_request", lambda request: "")
    with pytest.raises(lyrics.LyricsSearchError, match="空白搜索页面"):
        UtatenLyricsProvider().search("結び")


def test_utaten_valid_no_results_does_not_retry(monkeypatch) -> None:
    import krok_helper.lyrics as lyrics
    calls = []
    def load(request):
        calls.append(request.full_url)
        return '<table class="searchResult"><tr><th>header</th></tr></table>'
    monkeypatch.setattr(lyrics, "_load_text_from_request", load)
    assert UtatenLyricsProvider().search("不存在") == []
    assert len(calls) == 1


def test_utaten_falls_back_to_legacy_when_primary_is_empty(monkeypatch) -> None:
    import krok_helper.lyrics as lyrics

    calls: list[str] = []
    def load(request):
        calls.append(request.full_url)
        if "page=1" in request.full_url:
            return ""
        return (
            '<table class="searchResult"><tr><th>header</th></tr>'
            '<tr><td><p class="searchResult__title"><a href="/lyric/1/">NEON</a></p></td>'
            '<td class="searchResult__artist"><p><a href="/artist/1/">A2O LITE</a></p></td></tr></table>'
        )

    monkeypatch.setattr(lyrics, "_load_text_from_request", load)

    results = UtatenLyricsProvider()._search_once(title="NEON")

    assert [item["title"] for item in results] == ["NEON"]
    assert len(calls) == 2
    assert "page=1" in calls[0]
    assert "show_artists=1" in calls[1]


def test_utaten_popular_then_latest_order_survives_service_ranking(monkeypatch):
    from krok_helper.lyrics import LyricsSearchService
    provider = UtatenLyricsProvider()
    def entry(i, title):
        return {"url": f"https://utaten.com/lyric/{i}/", "title": title, "artist": "a", "snippet": ""}
    popular = [entry(1, "test live"), entry(2, "test remix"), entry(3, "test new"), entry(4, "test")]
    latest = [popular[0], popular[2], popular[1], popular[3]]
    monkeypatch.setattr(provider, "_search_once", lambda **kw: latest if kw.get("sort") == "release_date_desc" else popular)
    results = LyricsSearchService([provider]).search("test", provider_ids=("utaten",))
    assert [x.track_id for x in results] == ["1", "3", "4", "2"]
    assert [x.track_id for x in provider.search("test", limit=2, page=2)] == ["4", "2"]


def test_utaten_does_not_split_spaces_into_title_and_artist(monkeypatch):
    provider = UtatenLyricsProvider()
    seen: list[str] = []

    def search_once(*, title, **kwargs):
        seen.append(title)
        if title == "君の名は 主題歌":
            return [{"title": "君の名は 主題歌", "artist": "RADWIMPS", "snippet": "",
                     "url": "https://utaten.com/lyric/example/"}]
        return []

    monkeypatch.setattr(provider, "_search_once", search_once)
    results = provider.search("君の名は 主題歌")

    assert [(x.title, x.artist) for x in results] == [("君の名は 主題歌", "RADWIMPS")]
    assert seen == ["君の名は 主題歌"]


def test_utaten_filters_title_locally_with_exact_match(monkeypatch):
    import krok_helper.lyrics as lyrics
    page = '<table class="searchResult">'
    for i, title in enumerate(("君の名は 主題歌", "別の曲")):
        page += f'<tr><td><p class="searchResult__title"><a href="/lyric/{i}/">{title}</a></p></td><td class="searchResult__artist"><p><a href="/artist/{i}/">RADWIMPS</a></p></td></tr>'
    page += '</table>'
    monkeypatch.setattr(lyrics, "_load_text_from_request", lambda request: page)
    assert [x["title"] for x in UtatenLyricsProvider()._search_once(title="君の名は 主題歌")] == ["君の名は 主題歌"]


def test_utaten_sends_whole_title_with_spaces(monkeypatch):
    """标题里的空格不能被当成歌手分隔符，整串都原样作为歌名参数。"""
    from urllib.parse import urlencode
    import krok_helper.lyrics as lyrics

    calls: list[str] = []
    def load(request):
        calls.append(request.full_url)
        return (
            '<table class="searchResult"><tr><th>header</th></tr>'
            '<tr><td><p class="searchResult__title"><a href="/lyric/1/">君の名は 主題歌</a></p></td>'
            '<td class="searchResult__artist"><p><a href="/artist/1/">RADWIMPS</a></p></td></tr></table>'
        )

    monkeypatch.setattr(lyrics, "_load_text_from_request", load)

    results = UtatenLyricsProvider()._search_once(title="君の名は 主題歌")

    assert [item["title"] for item in results] == ["君の名は 主題歌"]
    assert len(calls) == 1
    assert urlencode({"title": "君の名は 主題歌"}) in calls[0]
    assert "artist_name=&" in calls[0]


def test_utaten_popular_failure_still_tries_latest_sort(monkeypatch):
    """人气排序整条失败时，不能直接报错，发行日期排序仍要走完。"""
    import krok_helper.lyrics as lyrics

    provider = UtatenLyricsProvider()

    def fake_variants(keyword, *, sort="popular_sort_asc"):
        if sort == "popular_sort_asc":
            raise lyrics.LyricsSearchError("popular failed")
        return [{"url": "https://utaten.com/lyric/2/", "title": "新曲", "artist": "a", "snippet": ""}]

    monkeypatch.setattr(provider, "_search_all_variants", fake_variants)

    results = provider.search("新曲")

    assert [item.track_id for item in results] == ["2"]
