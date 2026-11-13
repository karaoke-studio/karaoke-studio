from __future__ import annotations

from krok_helper.video_download.download_task import (
    SOURCE_BILIBILI,
    SOURCE_YOUTUBE,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DOWNLOADING,
    TASK_STATUS_FAILED,
    TASK_STATUS_WAITING,
)
from krok_helper.video_download.video_download_page import ParsedBatch, ParsedVideoGroup, ParseLinksWorker
from krok_helper.video_download.video_logic import classify_existing_task


def test_existing_completed_kept_as_is(make_download_task) -> None:
    task = make_download_task(status=TASK_STATUS_COMPLETED)

    assert classify_existing_task(task) == "keep"


def test_existing_downloading_kept_as_is(make_download_task) -> None:
    task = make_download_task(status=TASK_STATUS_DOWNLOADING)

    assert classify_existing_task(task) == "keep"


def test_existing_waiting_updates(make_download_task) -> None:
    for status in (TASK_STATUS_WAITING, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED):
        task = make_download_task(status=status)

        assert classify_existing_task(task) == "update"


def test_ParsedBatch_dataclass_default_groups_none() -> None:
    batch = ParsedBatch(infos=[], errors=[])

    assert batch.groups is None


def test_ParsedVideoGroup_holds_source_url_and_infos(make_video_info) -> None:
    info = make_video_info(source=SOURCE_BILIBILI)
    group = ParsedVideoGroup(source_url="https://www.bilibili.com/video/BV1abc", infos=[info])

    assert group.source_url == "https://www.bilibili.com/video/BV1abc"
    assert group.infos == [info]


def test_worker_accepts_bv_and_links_with_platform_cookies_and_multipart(monkeypatch) -> None:
    canonical = "https://www.bilibili.com/video/BV1tR5g6jE6Y/"
    youtube_url = "https://www.youtube.com/watch?v=abc"
    worker = ParseLinksWorker(
        ["BV1tR5g6jE6Y", youtube_url],
        {SOURCE_BILIBILI: "bili.txt", SOURCE_YOUTUBE: "youtube.txt"},
    )
    calls = []

    def extract(url, cookie_file, *, allow_playlist):
        calls.append((url, cookie_file, allow_playlist))
        if url == canonical:
            return {"title": "测试视频", "entries": [
                {"title": f"分 P {part}", "webpage_url": f"{canonical}?p={part}",
                 "formats": [{"format_id": "best", "ext": "mp4"}]}
                for part in (1, 2)
            ]}, ""
        return {"title": "YouTube 视频", "webpage_url": url}, ""

    monkeypatch.setattr(worker._service, "_extract_info_with_best_backend", extract)
    batches = []
    worker.batchFinished.connect(batches.append)

    worker.run()

    assert calls == [(canonical, "bili.txt", True), (youtube_url, "youtube.txt", False)]
    batch, = batches
    assert batch.errors == []
    assert len(batch.groups) == 2
    assert len(batch.groups[0].infos) == 2
    assert [info.url for info in batch.infos] == [f"{canonical}?p=1", f"{canonical}?p=2", youtube_url]
    assert [info.source for info in batch.infos] == [SOURCE_BILIBILI, SOURCE_BILIBILI, SOURCE_YOUTUBE]
