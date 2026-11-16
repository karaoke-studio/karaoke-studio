"""Resolve external Studio installations and own their AI dependency overlay."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import threading
from pathlib import Path

_prepare_lock = threading.Lock()


def discover_studio() -> Path | None:
    for root in (Path('/Applications'), Path.home() / 'Applications'):
        for bundle in sorted(root.glob('*.app')):
            try:
                with (bundle / 'Contents/Info.plist').open('rb') as stream:
                    info = plistlib.load(stream)
                if info.get('CFBundleIdentifier') == 'studio.pymss.desktop':
                    return bundle
            except (OSError, ValueError):
                continue
    return None


def resolve_external_python(selected: str | Path) -> Path:
    path = Path(selected).expanduser().absolute()
    if path.suffix.lower() != '.app' and not path.name.lower().startswith('python'):
        app = next((parent for parent in path.parents if parent.suffix.lower() == '.app'), None)
        if app is not None:
            path = app
    if path.suffix.lower() != '.app':
        if not path.is_file():
            raise ValueError('所选 PyMSS 解释器不存在，请重新关联环境。')
        if path.name.lower().startswith('python'):
            return path
        # Console scripts are not Python interpreters; the bridge must be
        # passed to their sibling interpreter, never to a desktop launcher.
        for sibling in ('python3', 'python'):
            python = path.parent / sibling
            if python.is_file():
                return python
        raise ValueError('请选择 PyMSS 的 Python 解释器或 Pymss Studio.app。')
    runtime = path / 'Contents/Resources/python-runtime'
    state = runtime / 'runtime-envs/active-runtime.json'
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding='utf-8'))
            raw = data['pythonPath']
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError('pythonPath 为空')
            python = Path(raw).expanduser()
            if not python.is_absolute():
                python = state.parent / python
            python = Path(os.path.abspath(python))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('Studio 活动运行时记录损坏，请先在 Studio 中修复环境。') from exc
        if not python.is_file():
            raise ValueError('Studio 的活动 Python 不存在，请先在 Studio 中修复环境。')
        return python
    for python in (runtime / 'bin/python3', runtime / 'bin/python'):
        if python.is_file():
            return python
    raise ValueError('此应用未包含可用的 PyMSS Python 运行时。')


def _source_records(python: Path) -> list[tuple[str, int, int]]:
    root = python.parent.parent.resolve()
    files = [python.resolve(), root / 'pyvenv.cfg', root / 'runtime-envs/active-runtime.json']
    roots = {root, python.resolve().parent.parent}
    config = root / 'pyvenv.cfg'
    if config.is_file():
        for line in config.read_text(encoding='utf-8').splitlines():
            key, _, value = line.partition('=')
            if key.strip() == 'home':
                roots.add(Path(value.strip()).parent.resolve())
    for source_root in sorted(roots):
        for site in source_root.glob('lib/python*/site-packages'):
            for pattern in ('pymss-*.dist-info/METADATA', 'pymss_core-*.dist-info/METADATA',
                            'torch-*.dist-info/METADATA'):
                files.extend(sorted(site.glob(pattern)))
    records = []
    for path in files:
        if path.is_file():
            stat = path.stat()
            records.append((str(path), stat.st_size, stat.st_mtime_ns))
    return records


def runtime_identity(python: Path) -> str:
    """Cheap fingerprint also invalidates overlays after in-place Studio updates."""
    digest = hashlib.sha256(str(python.absolute()).encode())
    digest.update(json.dumps(_source_records(python)).encode())
    return digest.hexdigest()[:24]


def ai_python_path(python: Path, work_root: Path) -> Path:
    return work_root / 'ai-environments' / runtime_identity(python) / 'bin/python3'


def prepared_ai_python(python: Path, work_root: Path) -> str | None:
    target = ai_python_path(python, work_root)
    marker = target.parent.parent / 'lin-k-ready.json'
    current = work_root / 'ai-environments/current'
    if target.is_file() and marker.is_file() and current.resolve() == target.parent.parent.resolve():
        return str(current / 'bin/python3')
    return None


def prepare_ai_python(python: Path, work_root: Path) -> str:
    """Run only on the backend executor, never in runtime_python()/the GUI thread.

    Copies prevent SUG's resolve()-based install logging from escaping into the
    external installation. pip belongs to this venv, while torch is inherited.
    """
    with _prepare_lock:
        ready = prepared_ai_python(python, work_root)
        if ready:
            return ready
        target = ai_python_path(python, work_root)
        root = target.parent.parent
        root.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
            env.pop(key, None)
        env['PYTHONNOUSERSITE'] = '1'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run(
            [str(python), '-m', 'venv', '--copies', '--system-site-packages', str(root)],
            env=env, capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f'无法创建 AI 打轴增量环境：{result.stderr[-2000:]}')
        # AI workers may start before the separation service. Refuse a stale
        # base even then, instead of mixing new Studio packages with old extras.
        guard = (
            'import os, sys\n'
            'sys.dont_write_bytecode = True\n'
            f'_records = {_source_records(python)!r}\n'
            'try:\n'
            '    for _path, _size, _mtime in _records:\n'
            '        _stat = os.stat(_path)\n'
            '        if (_stat.st_size, _stat.st_mtime_ns) != (_size, _mtime):\n'
            '            raise OSError("运行时已变化")\n'
            'except OSError:\n'
            '    sys.stderr.write("Studio 运行时已更新或移动，请在音频分离页重新关联，然后修复 AI 打轴环境。\\n")\n'
            '    sys.stderr.flush()\n'
            '    os._exit(78)\n'
        )
        sites = list(root.glob('lib/python*/site-packages'))
        if len(sites) != 1:
            raise RuntimeError('无法确定 AI 增量环境的依赖目录。')
        # Studio can activate its own venv. Python's venv module inherits the
        # base installation, not that venv's extra packages; expose those as
        # read-only fallback paths after this overlay's own site-packages.
        result = subprocess.run(
            [str(python), '-c', 'import json,site; print(json.dumps(site.getsitepackages()))'],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError('无法读取 Studio 活动环境的依赖目录。')
        source_sites = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(source_sites, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in source_sites):
            raise RuntimeError('Studio 返回了无效的依赖目录。')
        (sites[0] / 'lin_k_studio.pth').write_text(
            '\n'.join(p for p in source_sites if '\n' not in p and '\r' not in p) + '\n', encoding='utf-8'
        )
        (sites[0] / 'sitecustomize.py').write_text(guard, encoding='utf-8')
        # Confirm interpreter isolation before ever exposing it to SUG's installer.
        result = subprocess.run(
            [str(target), '-c', 'import json,sys,torch,pip; '
             'print(json.dumps([sys.prefix,sys.base_prefix,pip.__file__,torch.__version__]))'],
            env=env, capture_output=True, text=True, timeout=90,
        )
        if result.returncode:
            raise RuntimeError(f'AI 打轴增量环境检测失败：{result.stderr[-2000:]}')
        prefix, base, pip_file, torch_version = json.loads(result.stdout.strip().splitlines()[-1])
        if Path(prefix).resolve() != root.resolve() or prefix == base or not Path(pip_file).resolve().is_relative_to(root.resolve()):
            raise RuntimeError('AI 打轴环境未正确隔离，已阻止向 Studio 安装依赖。')
        marker = root / 'lin-k-ready.json'
        marker.write_text(json.dumps({'source': str(python), 'torch': torch_version}), encoding='utf-8')
        # SUG remembers an absolute path. Keep it stable across Studio updates,
        # while replacing the environment only after the new one passes checks.
        current = root.parent / 'current'
        pending = root.parent / 'current.pending'
        pending.unlink(missing_ok=True)
        pending.symlink_to(root.name, target_is_directory=True)
        pending.replace(current)
        return str(current / 'bin/python3')
