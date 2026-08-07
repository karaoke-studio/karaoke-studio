from __future__ import annotations

from pathlib import Path

from krok_helper.audio_processing.separation.cache import (
    IntermediateResultCache,
    cache_key,
    input_fingerprint,
)


def test_intermediate_cache_requires_matching_input_and_archive_digest(tmp_path) -> None:
    source = tmp_path / "song.wav"
    source.write_bytes(b"first-input")
    metadata = {
        "input": input_fingerprint(source),
        "preset_id": "harmony",
        "preset_version": 1,
        "model": "model-a",
    }
    key = cache_key(metadata)
    archive = tmp_path / "result.zip"
    archive.write_bytes(b"verified-result")
    cache = IntermediateResultCache(tmp_path / "cache")

    stored = cache.store(key, archive, metadata)
    assert cache.lookup(key) == stored

    stored.write_bytes(b"tampered-result")
    assert cache.lookup(key) is None

    source.write_bytes(b"second-input")
    changed = {**metadata, "input": input_fingerprint(source)}
    assert cache_key(changed) != key


def test_incomplete_cache_entry_is_never_reused(tmp_path) -> None:
    cache = IntermediateResultCache(tmp_path / "cache")
    entry = cache.root / ("a" * 64)
    entry.mkdir(parents=True)
    (entry / "result.zip").write_bytes(b"partial")

    assert cache.lookup("a" * 64) is None
