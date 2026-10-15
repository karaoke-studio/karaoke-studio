"""Release selection, stored byte ranges and independent patch authentication."""

import base64
from dataclasses import replace
import hashlib
import json
import plistlib
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

from krok_helper.updater.macos_feed import ASSET_NAME, DELTA_ASSET_NAME, DELTA_NS, FEED_NAME, MacDelta, MacFeed, render_feed
from scripts import build_macos_deltas as builder


def release(version, **overrides):
    return {'tag_name': 'v' + version, 'assets': [{'name': ASSET_NAME}, {'name': FEED_NAME}], **overrides}


def test_selects_latest_twenty_same_minor_using_numeric_four_component_versions():
    releases = [release(f'4.3.{i}') for i in range(30)]
    releases += [release('4.3.29.2'), release('4.3.29.10'), release('4.4.0'),
                 release('3.3.99'), release('4.3.31'), release('4.3.29.11', prerelease=True),
                 release('4.3.29.12', draft=True), release('4.3.29.13', assets=[])]
    selected = builder.select_baselines(releases, '4.3.30')
    assert len(selected) == 20
    assert [r['version'] for r in selected[:4]] == ['4.3.29.10', '4.3.29.2', '4.3.29', '4.3.28']
    assert selected[-1]['version'] == '4.3.12'
    assert builder.select_baselines(releases, '4.5.0') == []
    assert [r['version'] for r in builder.select_baselines(releases, '4.3.2')] == ['4.3.1', '4.3.0']


@pytest.mark.parametrize('assets', [
    [{'name': ASSET_NAME}],  # Pre-Sparkle and missing-key releases are both manual-only.
    [{'name': FEED_NAME}],
    [{'name': ASSET_NAME}, {'name': 'KaraokeStudio-windows.json'}],
    [],
])
def test_baseline_requires_both_macos_archive_and_signed_appcast(assets):
    history = [release('4.3.1'), release('4.3.2', assets=assets)]
    assert [r['version'] for r in builder.select_baselines(history, '4.3.3')] == ['4.3.1']


def test_manual_only_releases_do_not_consume_twenty_baseline_slots():
    history = [release(f'4.3.{i}') for i in range(25)]
    history += [release(f'4.3.{i}', assets=[{'name': ASSET_NAME}]) for i in range(25, 50)]
    selected = builder.select_baselines(history, '4.3.50')
    assert [r['version'] for r in selected] == [f'4.3.{i}' for i in range(24, 4, -1)]


def test_first_auto_update_release_keeps_full_feed_without_preparing_deltas(tmp_path, monkeypatch):
    from krok_helper.updater.macos_feed import parse_feed
    from krok_helper.updater.sources import build_download_url
    signature = base64.b64encode(bytes(64)).decode()
    feed = MacFeed('4.3.3', '4.3.3000', 4, signature, build_download_url('github', 'v4.3.3', ASSET_NAME))
    (tmp_path / FEED_NAME).write_bytes(render_feed(feed))
    (tmp_path / DELTA_ASSET_NAME).write_bytes(b'stale container')
    history = tmp_path / 'history.json'
    history.write_text(json.dumps([release('4.3.1', assets=[{'name': ASSET_NAME}]),
                                   release('4.3.2', assets=[{'name': ASSET_NAME}])]))
    monkeypatch.setattr(sys, 'argv', ['build_macos_deltas.py', str(tmp_path / ASSET_NAME), '--sdk-dir', str(tmp_path), '--releases-json', str(history)])
    monkeypatch.setenv('KROK_SPARKLE_PUBLIC_ED_KEY', 'test')
    monkeypatch.setenv('KROK_SPARKLE_PRIVATE_ED_KEY', 'test')
    monkeypatch.setattr(builder, 'read_app_version', lambda: '4.3.3')
    monkeypatch.setattr(builder, 'sign_file', lambda *a: signature)
    monkeypatch.setattr(builder, 'extract', lambda *a: pytest.fail('No baseline should require unpacking'))
    monkeypatch.setattr(builder, 'build_delta', lambda *a, **kw: pytest.fail('Manual-only baselines must not be downloaded'))
    builder.main()
    assert parse_feed((tmp_path / FEED_NAME).read_bytes(), tag='v4.3.3', version='4.3.3') == feed
    assert not (tmp_path / DELTA_ASSET_NAME).exists()


def test_container_ranges_preserve_each_independent_signature(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    patches = []
    for i, size in enumerate((2000, 80000, 3500)):
        path = tmp_path / f'{i}.delta'
        data = bytes([i]) * size
        path.write_bytes(data)
        patches.append((path, MacDelta(f'4.3.{i}', size, base64.b64encode(key.sign(data)).decode(),
                                     sha256=hashlib.sha256(data).hexdigest())))
    container = tmp_path / DELTA_ASSET_NAME
    rows = builder.pack_deltas(container, patches)
    raw = container.read_bytes()
    for (path, _), row in zip(patches, rows):
        segment = raw[row.offset:row.offset + row.length]
        assert segment == path.read_bytes()
        key.public_key().verify(base64.b64decode(row.signature), segment)
    with zipfile.ZipFile(container) as bundle:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in bundle.infolist())
    feed = MacFeed('4.3.3', '4.3.3000', 100, patches[0][1].signature, 'full.zip', rows, len(raw))
    index = ET.fromstring(render_feed(feed)).find(f'./channel/item/{{{DELTA_NS}}}deltas')
    assert len(index) == 3 and int(index.get('length')) == len(raw)
    assert index.get('url').endswith('/v4.3.3/' + DELTA_ASSET_NAME)


def test_missing_keys_remove_stale_container_without_fetching_history(tmp_path, monkeypatch):
    output = tmp_path / DELTA_ASSET_NAME
    output.write_bytes(b'stale')
    (tmp_path / FEED_NAME).write_bytes(b'stale signed index')
    monkeypatch.delenv('KROK_SPARKLE_PUBLIC_ED_KEY', raising=False)
    monkeypatch.delenv('KROK_SPARKLE_PRIVATE_ED_KEY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['build_macos_deltas.py', str(tmp_path / ASSET_NAME), '--sdk-dir', str(tmp_path)])
    monkeypatch.setattr(builder, 'fetch_releases', lambda: pytest.fail('Must not fetch without keys'))
    builder.main()
    assert not output.exists()
    assert not (tmp_path / FEED_NAME).exists()


@pytest.mark.parametrize('reuse_app', [False, True])
def test_only_successfully_verified_patches_enter_index(tmp_path, monkeypatch, reuse_app):
    from krok_helper.updater.sources import build_download_url
    signature = base64.b64encode(bytes(64)).decode()
    feed = MacFeed('4.3.3', '4.3.3000', 4, signature, build_download_url('github', 'v4.3.3', ASSET_NAME))
    (tmp_path / FEED_NAME).write_bytes(render_feed(feed))
    history = tmp_path / 'history.json'
    history.write_text(json.dumps([release('4.3.1'), release('4.3.2')]))
    argv = ['build_macos_deltas.py', str(tmp_path / ASSET_NAME), '--sdk-dir', str(tmp_path), '--releases-json', str(history)]
    target = tmp_path
    if reuse_app:
        target = tmp_path / 'dist/Lin-K Lyrics.app'
        (target / 'Contents').mkdir(parents=True)
        (target / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion': feed.build_version, 'SUPublicEDKey': 'test'}))
        argv += ['--app', str(target)]
    original_inventory = builder.inventory
    original = original_inventory(target) if reuse_app else None
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setenv('KROK_SPARKLE_PUBLIC_ED_KEY', 'test')
    monkeypatch.setenv('KROK_SPARKLE_PRIVATE_ED_KEY', 'test')
    monkeypatch.setattr(builder, 'read_app_version', lambda: '4.3.3')
    monkeypatch.setattr(builder, 'sign_file', lambda *a: signature)
    extracted = []
    def extract(archive, destination):
        assert not reuse_app, 'The built application must not require archive extraction'
        extracted.append(archive)
        return target
    monkeypatch.setattr(builder, 'extract', extract)
    monkeypatch.setattr(builder, 'inventory', lambda *a: {})
    def build(baseline, **kwargs):
        assert kwargs['target'] == target
        if baseline['version'] == '4.3.2':
            raise ValueError('failed apply verification')
        patch = tmp_path / 'good.delta'
        patch.write_bytes(b'good')
        return patch, MacDelta('4.3.1', 4, signature, sha256=hashlib.sha256(b'good').hexdigest())
    monkeypatch.setattr(builder, 'build_delta', build)
    builder.main()
    index = ET.fromstring((tmp_path / FEED_NAME).read_bytes()).find(f'./channel/item/{{{DELTA_NS}}}deltas')
    assert [e.get('from') for e in index] == ['4.3.1']
    assert extracted == ([] if reuse_app else [tmp_path / ASSET_NAME])
    if reuse_app:
        assert original_inventory(target) == original


@pytest.mark.parametrize('invalid', ['missing', 'not_app', 'version', 'key'])
def test_reused_target_must_match_release_before_generating_patches(tmp_path, monkeypatch, invalid):
    from krok_helper.updater.sources import build_download_url
    signature = base64.b64encode(bytes(64)).decode()
    feed = MacFeed('4.3.3', '4.3.3000', 4, signature, build_download_url('github', 'v4.3.3', ASSET_NAME))
    (tmp_path / FEED_NAME).write_bytes(render_feed(feed))
    target = tmp_path / ('target' if invalid == 'not_app' else 'Lin-K Lyrics.app')
    if invalid != 'missing':
        (target / 'Contents').mkdir(parents=True)
        (target / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleVersion': '4.3.2000' if invalid == 'version' else feed.build_version,
            'SUPublicEDKey': 'other' if invalid == 'key' else 'test',
        }))
    monkeypatch.setattr(sys, 'argv', ['build_macos_deltas.py', str(tmp_path / ASSET_NAME), '--sdk-dir', str(tmp_path), '--app', str(target)])
    monkeypatch.setenv('KROK_SPARKLE_PUBLIC_ED_KEY', 'test')
    monkeypatch.setenv('KROK_SPARKLE_PRIVATE_ED_KEY', 'test')
    monkeypatch.setattr(builder, 'read_app_version', lambda: feed.version)
    monkeypatch.setattr(builder, 'sign_file', lambda *a: signature)
    monkeypatch.setattr(builder, 'fetch_releases', lambda: [release('4.3.2')])
    monkeypatch.setattr(builder, 'extract', lambda *a: pytest.fail('An invalid explicit target must not fall back to extraction'))
    monkeypatch.setattr(builder, 'build_delta', lambda *a, **kw: pytest.fail('Invalid target must be rejected before patch generation'))
    with pytest.raises(ValueError, match='Target application'):
        builder.main()
    assert not (tmp_path / DELTA_ASSET_NAME).exists()


def test_history_failure_removes_stale_delta_index_and_retains_full_update(tmp_path, monkeypatch):
    from krok_helper.updater.macos_feed import parse_feed
    from krok_helper.updater.sources import build_download_url
    signature = base64.b64encode(bytes(64)).decode()
    feed = MacFeed('4.3.3', '4.3.3000', 4, signature, build_download_url('github', 'v4.3.3', ASSET_NAME),
                   (MacDelta('4.3.2', 4, signature, sha256='0' * 64),), 4)
    (tmp_path / FEED_NAME).write_bytes(render_feed(feed))
    (tmp_path / DELTA_ASSET_NAME).write_bytes(b'stale')
    monkeypatch.setattr(sys, 'argv', ['build_macos_deltas.py', str(tmp_path / ASSET_NAME), '--sdk-dir', str(tmp_path)])
    monkeypatch.setenv('KROK_SPARKLE_PUBLIC_ED_KEY', 'test')
    monkeypatch.setenv('KROK_SPARKLE_PRIVATE_ED_KEY', 'test')
    monkeypatch.setattr(builder, 'read_app_version', lambda: '4.3.3')
    monkeypatch.setattr(builder, 'sign_file', lambda *a: signature)
    def fail():
        raise OSError('GitHub unavailable')
    monkeypatch.setattr(builder, 'fetch_releases', fail)
    builder.main()
    parsed = parse_feed((tmp_path / FEED_NAME).read_bytes(), tag='v4.3.3', version='4.3.3')
    assert parsed.signature == signature and not parsed.deltas
    assert not (tmp_path / DELTA_ASSET_NAME).exists()
