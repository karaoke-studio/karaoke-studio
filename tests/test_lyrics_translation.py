from __future__ import annotations

import base64
import json

from krok_helper.lyrics import LyricsSearchCandidate, QqMusicLyricsProvider, _convert_krc_text


def test_convert_krc_extracts_language_translation() -> None:
    language_payload = {
        "content": [
            {"type": 0, "language": 0, "lyricContent": [["he ", "llo"], ["world"]]},
            {"type": 1, "language": 0, "lyricContent": [["你好"], ["世界"]]},
        ],
        "version": 1,
    }
    language_tag = base64.b64encode(json.dumps(language_payload).encode("utf-8")).decode("ascii")
    raw_krc = "\n".join(
        [
            f"[language:{language_tag}]",
            "[1000,2000]<0,500,0>Hello",
            "[3000,2000]<0,500,0>World",
        ]
    )

    _line_lyrics, _verbatim_lyrics, _plain_lyrics, translation_lyrics = _convert_krc_text(raw_krc)

    assert translation_lyrics.splitlines() == ["[00:01.00]你好", "[00:03.00]世界"]


def test_qq_fetch_lyrics_falls_back_to_play_lyric_info_translation(monkeypatch) -> None:
    encoded_translation = base64.b64encode(
        "[00:10.10]//\n[00:21.30]爱恨交织 在心里翻腾".encode("utf-8")
    ).decode("ascii")
    seen_payloads: list[dict] = []

    def fake_load_json(request):
        if "fcg_query_lyric_new.fcg" in request.full_url:
            return {"lyric": "[00:01.00]AIZO - King Gnu", "trans": ""}
        payload = json.loads(request.data.decode("utf-8"))
        seen_payloads.append(payload)
        return {"req": {"data": {"trans": encoded_translation}}}

    monkeypatch.setattr("krok_helper.lyrics._load_json_from_request", fake_load_json)
    candidate = LyricsSearchCandidate(
        provider_id="qm",
        provider_name="QQ音乐",
        track_id="632959788",
        title="AIZO",
        artist="King Gnu",
        album="AIZO",
        duration_seconds=215,
        provider_payload={"id": "632959788", "mid": "004Q1MO7275WXv"},
    )

    loaded = QqMusicLyricsProvider().fetch_lyrics(candidate)

    assert loaded.translation_lyrics == "[00:21.30]爱恨交织 在心里翻腾"
    assert seen_payloads[0]["req"]["param"] == {
        "songMID": "004Q1MO7275WXv",
        "songID": 632959788,
        "qrc": 0,
        "trans": 1,
        "roma": 0,
    }


def test_search_business_errors_are_not_empty_results(monkeypatch):
    import pytest
    import krok_helper.lyrics as lyrics
    for cls, payload in [
        (lyrics.QqMusicLyricsProvider, {"code": 429}),
        (lyrics.NeteaseLyricsProvider, {"code": 405}),
        (lyrics.KugouLyricsProvider, {"status": 0, "error_code": 20001}),
    ]:
        monkeypatch.setattr(lyrics, "_load_json_from_request", lambda request: payload)
        with pytest.raises(lyrics.LyricsSearchError, match="接口返回错误"):
            cls().search("test")


def test_qq_primary_failure_uses_backup(monkeypatch):
    import krok_helper.lyrics as lyrics
    def load(request):
        if "fcg_query_lyric_new" in request.full_url:
            raise lyrics.LyricsSearchError("HTTP 503")
        return {"code": 0, "req": {"code": 0, "data": {"lyric": "[00:01.00]hello"}}}
    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    candidate = LyricsSearchCandidate("qm", "QQ音乐", "1", "test", "artist", "", None,
                                      provider_payload={"mid": "mid", "id": "1"})
    assert QqMusicLyricsProvider().fetch_lyrics(candidate).line_lyrics == "[00:01.00]hello"


def test_search_batch_preserves_partial_errors():
    import krok_helper.lyrics as lyrics
    class Good:
        provider_id = "qm"
        provider_name = "QQ音乐"
        def search(self, *args, **kwargs):
            return [LyricsSearchCandidate("qm", "QQ音乐", "1", "test", "artist", "", None)]
    class Bad:
        provider_id = "ne"
        provider_name = "网易云音乐"
        def search(self, *args, **kwargs):
            raise lyrics.LyricsSearchError("网易云音乐：HTTP 503")
    batch = lyrics.LyricsSearchService([Good(), Bad()]).search_batch("test")
    assert len(batch.results) == 1
    assert batch.errors == ("网易云音乐：HTTP 503",)


def test_search_batch_allows_artist_only_with_empty_keyword():
    import krok_helper.lyrics as lyrics
    seen: dict[str, str] = {}

    class Utaten:
        provider_id = "utaten"
        provider_name = "UtaTen"
        def search(self, keyword, *, limit=25, page=1, artist=""):
            seen["keyword"] = keyword
            seen["artist"] = artist
            return [LyricsSearchCandidate("utaten", "UtaTen", "1", "結び", artist, "", None)]

    batch = lyrics.LyricsSearchService([Utaten()]).search_batch(
        "", provider_ids=("utaten",), artist="Ayumu Imazu"
    )

    assert seen == {"keyword": "", "artist": "Ayumu Imazu"}
    assert [item.title for item in batch.results] == ["結び"]


def test_search_batch_rejects_artist_without_utaten():
    import pytest
    import krok_helper.lyrics as lyrics

    with pytest.raises(lyrics.LyricsSearchError, match="仅支持 UtaTen"):
        lyrics.LyricsSearchService().search_batch(
            "", provider_ids=("qm",), artist="Ayumu Imazu"
        )


def test_search_batch_passes_artist_only_to_utaten():
    import krok_helper.lyrics as lyrics
    seen: dict[str, str] = {}

    class Utaten:
        provider_id = "utaten"
        provider_name = "UtaTen"
        def search(self, keyword, *, limit=25, page=1, artist=""):
            seen["utaten"] = artist
            return [LyricsSearchCandidate("utaten", "UtaTen", "1", keyword, artist, "", None)]

    class Other:
        provider_id = "qm"
        provider_name = "QQ音乐"
        def search(self, keyword, *, limit=25, page=1):
            seen["qm"] = "called"
            return [LyricsSearchCandidate("qm", "QQ音乐", "2", keyword, "", "", None)]

    batch = lyrics.LyricsSearchService([Utaten(), Other()]).search_batch(
        "結び", provider_ids=("utaten", "qm"), artist="Ayumu Imazu"
    )

    assert seen == {"utaten": "Ayumu Imazu", "qm": "called"}
    assert len(batch.results) == 2


def test_search_batch_all_fail_returns_empty_batch_with_errors():
    """所有来源失败也要返回 batch，让页面用 InfoBar 反馈而不是抛异常弹窗。"""
    import krok_helper.lyrics as lyrics
    class Bad:
        provider_id = "ne"
        provider_name = "网易云音乐"
        def search(self, *args, **kwargs):
            raise lyrics.LyricsSearchError("网易云音乐：HTTP 503")
    batch = lyrics.LyricsSearchService([Bad()]).search_batch("test")
    assert batch.results == []
    assert batch.errors == ("网易云音乐：HTTP 503",)
