"""Build verified, independently signed direct Sparkle deltas in one stored ZIP."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from krok_helper.updater.macos_feed import (
    ASSET_NAME, DELTA_ASSET_NAME, FEED_NAME, MAX_DELTAS, MacDelta,
    macos_versions, parse_feed, render_feed,
)
from scripts.build_macos_appcast import sign_file
from scripts.update_macos_version import read_app_version


def version_key(version: str) -> tuple[int, int, int, int]:
    macos_versions(version)
    return tuple(int(p) for p in (version.split('.') + ['0'])[:4])


def select_baselines(releases: list[dict], version: str) -> list[dict]:
    target = version_key(version)
    selected = {}
    for release in releases:
        if release.get('draft') or release.get('prerelease'):
            continue
        tag = release.get('tag_name', '')
        if not tag.startswith('v'):
            continue
        try:
            key = version_key(tag[1:])
        except ValueError:
            continue
        if key[:2] != target[:2] or key >= target:
            continue
        assets = release.get('assets', [])
        asset = next((a for a in assets if a['name'] == ASSET_NAME), None)
        # Manual-only releases have no signed appcast and cannot use deltas.
        if asset and any(a['name'] == FEED_NAME for a in assets):
            selected[key] = {'version': tag[1:], 'asset': asset}
    return [selected[key] for key in sorted(selected, reverse=True)[:MAX_DELTAS]]


def fetch_releases() -> list[dict]:
    releases = []
    for page in range(1, 1001):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Lin-K-macos-deltas'}
        token = os.environ.get('GH_TOKEN', '').strip()
        if token:
            headers['Authorization'] = f'Bearer {token}'
        url = f'https://api.github.com/repos/karaoke-studio/karaoke-studio/releases?per_page=100&page={page}'
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            batch = json.load(response)
        releases.extend(batch)
        if len(batch) < 100:
            return releases
    raise RuntimeError('Release pagination did not finish')


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(app: Path) -> dict:
    entries = {}
    for path in [app, *app.rglob('*')]:
        mode = stat.S_IMODE(path.lstat().st_mode)
        name = path.relative_to(app).as_posix()
        if path.is_symlink():
            entries[name] = ('symlink', mode, os.readlink(path))
        elif path.is_dir():
            entries[name] = ('directory', mode)
        elif path.is_file():
            entries[name] = ('file', mode, path.stat().st_size, sha256(path))
        else:
            raise ValueError(f'Unsupported file type: {name}')
    return entries


def extract(archive: Path, destination: Path) -> Path:
    subprocess.run(['ditto', '-x', '-k', str(archive), str(destination)], check=True, capture_output=True)
    app = destination / 'Lin-K Lyrics.app'
    if not app.is_dir() or app.is_symlink():
        raise ValueError('Archive does not contain Lin-K Lyrics.app')
    return app


def build_delta(baseline: dict, *, target: Path, target_inventory: dict, work: Path,
                sdk: Path, public: str, private: str) -> tuple[Path, MacDelta]:
    version, asset = baseline['version'], baseline['asset']
    directory = work / version
    directory.mkdir()
    archive = directory / ASSET_NAME
    expected = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected):
        raise ValueError('Historical asset has no SHA256 digest')
    started = time.monotonic()
    # Public asset downloads never receive the GitHub API authorization header.
    with urlopen(asset['browser_download_url'], timeout=60) as response, archive.open('wb') as output:
        shutil.copyfileobj(response, output)
    if archive.stat().st_size != asset['size'] or 'sha256:' + sha256(archive) != expected:
        raise ValueError('Historical archive failed size/SHA256 validation')
    old = extract(archive, directory / 'old')
    info = plistlib.loads((old / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleVersion') != macos_versions(version)[1]:
        raise ValueError('Historical bundle does not use the supported build version mapping')
    patch = directory / f'{version}.delta'
    for arguments in (
        ['create', '--version', '4', '--compression', 'default', str(old), str(target), str(patch)],
        ['apply', str(old), str(directory / 'patched.app'), str(patch)],
    ):
        subprocess.run([str(sdk / 'bin/BinaryDelta'), *arguments], check=True,
                       capture_output=True, timeout=600)
    if inventory(directory / 'patched.app') != target_inventory:
        raise ValueError('Applied delta differs from the target application')
    signature = sign_file(patch, sdk, public, private)
    row = MacDelta(version, patch.stat().st_size, signature, sha256=sha256(patch))
    shutil.rmtree(old.parent)
    shutil.rmtree(directory / 'patched.app')
    archive.unlink()
    print(f'Delta {version}: {row.length} bytes, verified, {time.monotonic() - started:.1f}s', flush=True)
    return patch, row


def pack_deltas(output: Path, patches: list[tuple[Path, MacDelta]]) -> tuple[MacDelta, ...]:
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for patch, row in patches:
            bundle.write(patch, f'{row.from_version}.delta')
    rows = []
    with zipfile.ZipFile(output) as bundle, output.open('rb') as stream:
        for patch, row in patches:
            entry = bundle.getinfo(f'{row.from_version}.delta')
            stream.seek(entry.header_offset)
            header = stream.read(30)
            name_size, extra_size = struct.unpack_from('<HH', header, 26)
            offset = entry.header_offset + 30 + name_size + extra_size
            stream.seek(offset)
            data = stream.read(row.length)
            if entry.compress_type != zipfile.ZIP_STORED or hashlib.sha256(data).hexdigest() != row.sha256:
                raise ValueError('Stored delta range differs from the verified patch')
            rows.append(replace(row, offset=offset))
    return tuple(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--sdk-dir', type=Path, required=True)
    parser.add_argument('--app', type=Path, help='Reuse the built target .app instead of extracting the archive')
    parser.add_argument('--releases-json', type=Path, help='Use saved release metadata for reproducible local validation')
    args = parser.parse_args()
    archive = args.archive.resolve()
    output = archive.with_name(DELTA_ASSET_NAME)
    output.unlink(missing_ok=True)
    public = os.environ.get('KROK_SPARKLE_PUBLIC_ED_KEY', '').strip()
    private = os.environ.get('KROK_SPARKLE_PRIVATE_ED_KEY', '').strip()
    if not public or not private:
        archive.with_name(FEED_NAME).unlink(missing_ok=True)
        print('Sparkle keys are incomplete; skipping macOS delta generation.')
        return
    version = read_app_version()
    feed_path = archive.with_name(FEED_NAME)
    feed = parse_feed(feed_path.read_bytes(), tag=f'v{version}', version=version)
    # Fail before any expensive work if signing is misconfigured.
    sign_file(archive, args.sdk_dir, public, private)
    feed = replace(feed, deltas=(), delta_container_length=0)
    feed_path.write_bytes(render_feed(feed))
    releases = json.loads(args.releases_json.read_text()) if args.releases_json else None
    if releases is None:
        try:
            releases = fetch_releases()
        except Exception as exc:
            print(f'No deltas: failed to fetch historical releases ({type(exc).__name__}); full update retained.')
            return
    baselines = select_baselines(releases, version)
    print(f'Selected {len(baselines)} same-minor baselines (maximum {MAX_DELTAS}).', flush=True)
    if not baselines:
        print('No auto-update baselines; full update retained.')
        return
    with tempfile.TemporaryDirectory(prefix='lin-k-deltas-') as temporary:
        work = Path(temporary)
        if args.app is not None:
            target = args.app.resolve()
            if not target.is_dir() or target.suffix != '.app':
                raise ValueError('Target application must be an existing .app directory')
            info = plistlib.loads((target / 'Contents/Info.plist').read_bytes())
            if info.get('CFBundleVersion') != feed.build_version or info.get('SUPublicEDKey') != public:
                raise ValueError('Target application version or public key differs from this release')
        else:
            target = extract(archive, work / 'target')
        target_inventory = inventory(target)
        patches = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = {pool.submit(build_delta, b, target=target, target_inventory=target_inventory,
                                 work=work, sdk=args.sdk_dir.resolve(), public=public, private=private): b['version']
                     for b in baselines}
            for task in as_completed(tasks):
                try:
                    patches.append(task.result())
                except Exception as exc:
                    # Subprocess diagnostics may contain signing input; report
                    # only the exception class, never captured tool output.
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    print(f'Skipped delta {tasks[task]}: {reason}; full update retained.', flush=True)
        if not patches:
            print('No verified deltas; full update retained.')
            return
        patches.sort(key=lambda p: version_key(p[1].from_version), reverse=True)
        rows = pack_deltas(output, patches)
        feed_path.write_bytes(render_feed(replace(feed, deltas=rows, delta_container_length=output.stat().st_size)))
        print(f'Published delta index: {len(rows)} patches, {output.stat().st_size} stored bytes.', flush=True)


if __name__ == '__main__':
    main()
