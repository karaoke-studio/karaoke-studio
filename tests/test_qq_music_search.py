"""QQ web search response parsing, pagination, and lyric handoff."""

from urllib.parse import parse_qs, urlparse

import pytest

import krok_helper.lyrics as lyrics


def song(song_id=97773, title="晴天"):
    return {
        "songid": song_id, "songmid": f"mid-{song_id}", "songname": title,
        "singer": [{"name": "歌手一"}, {"name": "歌手二"}],
        "albumname": "专辑", "interval": 269,
    }


def response(songs):
    return {"code": 0, "data": {"song": {"list": songs}}}


def test_web_search_parses_real_response_shape_and_builds_an_anonymous_request(monkeypatch):
    requests = []

    def load(request):
        requests.append(request)
        return response([song()])

    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    results = lyrics.QqMusicLyricsProvider().search("晴天 & King Gnu / テーゼ", limit=3)
    assert len(results) == 1
    candidate = results[0]
    assert candidate.track_id == "97773"
    assert candidate.title == "晴天"
    assert candidate.artist == "歌手一 / 歌手二"
    assert candidate.album == "专辑"
    assert candidate.duration_seconds == 269
    assert candidate.provider_payload == {"id": "97773", "mid": "mid-97773"}
    request = requests[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Referer") == "https://y.qq.com/"
    url = urlparse(request.full_url)
    assert url.hostname == "c.y.qq.com"
    assert url.path == "/soso/fcgi-bin/client_search_cp"
    query = parse_qs(url.query)
    assert query == {"w": ["晴天 & King Gnu / テーゼ"], "p": ["1"], "n": ["25"],
                     "format": ["json"], "platform": ["yqq.json"], "ct": ["24"], "t": ["0"]}
    assert "new_json" not in query  # This mode silently yields empty results.


def test_web_search_paginates_without_skipping_original_positions(monkeypatch):
    pages = []

    def load(request):
        page = int(parse_qs(urlparse(request.full_url).query)["p"][0])
        pages.append(page)
        return response([song((page - 1) * 25 + index) for index in range(1, 26)])

    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    results = lyrics.QqMusicLyricsProvider().search("晴天", limit=28)
    assert pages == [1, 2]
    assert [candidate.track_id for candidate in results] == [str(index) for index in range(1, 29)]
    assert [candidate.provider_position for candidate in results] == list(range(1, 29))


def test_web_search_starts_from_requested_page_and_stops_on_short_page(monkeypatch):
    pages = []

    def load(request):
        pages.append(int(parse_qs(urlparse(request.full_url).query)["p"][0]))
        return response([song(26), song(27)])

    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    results = lyrics.QqMusicLyricsProvider().search("晴天", limit=50, page=2)
    assert pages == [2]
    assert [candidate.provider_position for candidate in results] == [26, 27]


def test_empty_song_list_is_a_valid_empty_search(monkeypatch):
    monkeypatch.setattr(lyrics, "_load_json_from_request", lambda request: response([]))
    assert lyrics.QqMusicLyricsProvider().search("无匹配结果") == []


@pytest.mark.parametrize("payload", [
    [], {"code": 2001}, {"code": 429},
])
def test_business_errors_are_reported_instead_of_becoming_empty_results(monkeypatch, payload):
    monkeypatch.setattr(lyrics, "_load_json_from_request", lambda request: payload)
    with pytest.raises(lyrics.LyricsSearchError, match="QQ音乐"):
        lyrics.QqMusicLyricsProvider().search("晴天")


@pytest.mark.parametrize("data", [None, [], {}, {"song": None}, {"song": []}, {"song": {"list": {}}}])
def test_malformed_song_responses_are_not_treated_as_no_matches(monkeypatch, data):
    payload = {"code": 0, "data": data}
    monkeypatch.setattr(lyrics, "_load_json_from_request", lambda request: payload)
    with pytest.raises(lyrics.LyricsSearchError, match="无效.*歌曲"):
        lyrics.QqMusicLyricsProvider().search("晴天")


def test_network_failure_keeps_the_provider_name(monkeypatch):
    def load(request):
        raise lyrics.LyricsSearchError("HTTP 503")

    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    with pytest.raises(lyrics.LyricsSearchError, match="QQ音乐.*HTTP 503"):
        lyrics.QqMusicLyricsProvider().search("晴天")


def test_search_result_passes_songmid_into_lyric_loading(monkeypatch):
    def load(request):
        if "fcg_query_lyric_new.fcg" in request.full_url:
            query = parse_qs(urlparse(request.full_url).query)
            assert query["songmid"] == ["mid-97773"]
            return {"code": 0, "lyric": "[00:01.00]测试歌词", "trans": "[00:01.00]测试翻译"}
        return response([song()])

    monkeypatch.setattr(lyrics, "_load_json_from_request", load)
    provider = lyrics.QqMusicLyricsProvider()
    candidate = provider.search("晴天", limit=1)[0]
    loaded = provider.fetch_lyrics(candidate)
    assert loaded.lyrics_loaded
    assert loaded.line_lyrics == "[00:01.00]测试歌词"
    assert loaded.translation_lyrics == "[00:01.00]测试翻译"


def test_qq_search_batch_keeps_more_results_available_for_the_ui(monkeypatch):
    monkeypatch.setattr(lyrics, "_load_json_from_request",
                        lambda request: response([song(index) for index in range(1, 26)]))
    batch = lyrics.LyricsSearchService([lyrics.QqMusicLyricsProvider()]).search_batch("晴天", provider_ids=("qm",))
    assert len(batch.results) == 25
    assert batch.errors == ()
    assert batch.has_more
    assert batch.next_provider_pages == {"qm": 2}
