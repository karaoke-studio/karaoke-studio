import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from krok_helper.audio_processing.separation import studio_runtime as runtime
from krok_helper.audio_processing.separation.real_backend import RealSeparationBackend
from krok_helper.audio_processing.separation.ai_timing_host import KaraokeAiTimingHost


def bundle(tmp_path):
    app = tmp_path / '改名 Studio.app'
    root = app / 'Contents/Resources/python-runtime'
    python = root / 'bin/python3'
    python.parent.mkdir(parents=True)
    python.write_text('python')
    state = root / 'runtime-envs/active-runtime.json'
    state.parent.mkdir()
    state.write_text(json.dumps({'pythonPath': '../bin/python3'}))
    return app, python, state


def test_bundle_resolves_active_runtime_and_moved_application(tmp_path):
    app, python, state = bundle(tmp_path)
    assert runtime.resolve_external_python(app) == python
    moved = tmp_path / 'another.app'
    app.rename(moved)
    assert runtime.resolve_external_python(moved) == moved / python.relative_to(app)


def test_active_runtime_is_authoritative(tmp_path):
    app, python, state = bundle(tmp_path)
    other = tmp_path / 'external/bin/python3'
    other.parent.mkdir(parents=True)
    other.touch()
    state.write_text(json.dumps({'pythonPath': str(other)}))
    assert runtime.resolve_external_python(app) == other
    other.unlink()
    with pytest.raises(ValueError, match='活动 Python'):
        runtime.resolve_external_python(app)


def test_broken_state_does_not_silently_fall_back(tmp_path):
    app, _, state = bundle(tmp_path)
    state.write_text('invalid')
    with pytest.raises(ValueError, match='记录损坏'):
        runtime.resolve_external_python(app)


def test_legacy_bundle_and_explicit_interpreter(tmp_path):
    app, python, state = bundle(tmp_path)
    state.unlink()
    assert runtime.resolve_external_python(app) == python
    assert runtime.resolve_external_python(python) == python


def test_desktop_launcher_resolves_to_runtime_without_launching_gui(tmp_path):
    app, python, _ = bundle(tmp_path)
    launcher = app / 'Contents/MacOS/pymss-studio'
    launcher.parent.mkdir()
    launcher.touch()
    assert runtime.resolve_external_python(launcher) == python


def test_console_script_uses_sibling_python(tmp_path):
    _, python, _ = bundle(tmp_path)
    script = tmp_path / 'bin/pymss'
    script.parent.mkdir()
    script.touch()
    sibling = script.parent / 'python3'
    sibling.touch()
    assert runtime.resolve_external_python(script) == sibling


def test_overlay_invalidated_when_studio_changes(tmp_path):
    _, python, state = bundle(tmp_path)
    work = tmp_path / 'lin-k'
    target = runtime.ai_python_path(python, work)
    target.parent.mkdir(parents=True)
    target.touch()
    (target.parent.parent / 'lin-k-ready.json').write_text('{}')
    current = target.parent.parent.parent / 'current'
    current.symlink_to(target.parent.parent, target_is_directory=True)
    assert runtime.prepared_ai_python(python, work) == str(current / 'bin/python3')
    state.write_text(json.dumps({'pythonPath': '../bin/python3', 'revision': 2}))
    assert runtime.prepared_ai_python(python, work) is None


@pytest.mark.parametrize('platform,version,expected', [
    ('darwin', '2.3.5', True), ('win32', '2.3.5', False),
    ('darwin', '2.0.18', True), ('darwin', '2.3.6', False),
])
def test_external_version_allowlist(monkeypatch, platform, version, expected):
    from krok_helper.audio_processing.separation import real_backend
    monkeypatch.setattr(real_backend.sys, 'platform', platform)
    assert RealSeparationBackend._external_version_supported(version) is expected


def test_host_exposes_overlay_without_starting_or_installing(tmp_path):
    class Backend:
        def external_ai_python(self):
            return str(tmp_path / 'ai/bin/python3')

        def snapshot(self):
            raise AssertionError('external AI runtime does not require a managed install')

    host = KaraokeAiTimingHost(Backend(), tmp_path)
    assert host.runtime_python() == str(tmp_path / 'ai/bin/python3')


def test_macos_uses_user_data_for_external_work(monkeypatch, tmp_path):
    from krok_helper.audio_processing.separation import real_backend
    monkeypatch.setattr(real_backend.sys, 'platform', 'darwin')
    monkeypatch.setenv('KARAOKE_STUDIO_SETTINGS_DIR', str(tmp_path))
    backend = RealSeparationBackend({})
    try:
        assert backend._external_work_root() == tmp_path / 'PyMSSExternal'
    finally:
        backend.shutdown()


@pytest.mark.parametrize('escaped_pip', [False, True])
def test_prepare_checks_isolation_before_exposing_python(monkeypatch, tmp_path, escaped_pip):
    _, python, _ = bundle(tmp_path)
    work = tmp_path / 'work'
    commands = []
    real_run = runtime.subprocess.run

    def run(command, **kwargs):
        commands.append(command)
        target = runtime.ai_python_path(python, work)
        root = target.parent.parent
        if '-m' in command:
            assert command[0] == str(python)
            assert '--copies' in command and '--system-site-packages' in command
            target.parent.mkdir(parents=True)
            target.touch()
            (root / 'lib/python3.12/site-packages').mkdir(parents=True)
            output = ''
        elif 'site.getsitepackages' in command[-1]:
            output = json.dumps([str(python.parent.parent / 'lib/python3.12/site-packages')])
        else:
            pip_file = python.parent / 'pip' if escaped_pip else root / 'lib/python3.12/site-packages/pip/__init__.py'
            output = json.dumps([str(root), str(python.parent.parent), str(pip_file), '2.7.1'])
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    monkeypatch.setattr(runtime.subprocess, 'run', run)
    if escaped_pip:
        with pytest.raises(RuntimeError, match='未正确隔离'):
            runtime.prepare_ai_python(python, work)
        assert runtime.prepared_ai_python(python, work) is None
    else:
        result = runtime.prepare_ai_python(python, work)
        assert Path(result).is_relative_to(work)
        assert Path(result).resolve() != python.resolve()
        assert runtime.prepare_ai_python(python, work) == result
        assert len(commands) == 3
        guard = Path(result).resolve().parent.parent / 'lib/python3.12/site-packages/sitecustomize.py'
        assert real_run([sys.executable, str(guard)], capture_output=True).returncode == 0
        python.write_text('updated Studio interpreter')
        rejected = real_run([sys.executable, str(guard)], capture_output=True)
        assert rejected.returncode == 78
        assert '重新关联'.encode() in rejected.stderr


def test_ai_adapter_installs_alignment_only_in_owned_overlay(monkeypatch, tmp_path):
    from krok_helper.audio_processing.separation.studio_ai import configure_runtime
    from strange_uta_game.backend.application.ai_timing import runtime as sug_runtime

    exe = tmp_path / 'overlay/bin/python3'
    exe.parent.mkdir(parents=True)
    exe.touch()
    (exe.parent.parent / 'lin-k-ready.json').write_text('{}')
    monkeypatch.setattr(sug_runtime, 'detect_torch_build', lambda _: ('2.7.1', 'cpu'))
    manager = sug_runtime.AiRuntimeManager()
    calls = []
    monkeypatch.setattr(manager, '_pip_install_requirements', lambda exe, args, **kw: calls.append(args))
    configure_runtime(manager)
    manager.install_shared(str(exe))
    args = calls[0]
    assert 'torchaudio==2.7.1' in args
    assert sug_runtime.PINNED_TRANSFORMERS in args
    assert not any('audio-separator' in arg for arg in args)
    assert '--extra-index-url' not in args  # Mac wheels are served by PyPI.


def test_ai_adapter_preserves_unowned_runtime_behavior(tmp_path):
    from krok_helper.audio_processing.separation.studio_ai import configure_runtime

    calls = []
    manager = SimpleNamespace(
        probe=lambda *a, **kw: calls.append(('probe', a)),
        install_shared=lambda *a, **kw: calls.append(('install', a)),
    )
    configure_runtime(manager)
    manager.probe('/external/python')
    manager.install_shared('/external/python')
    assert [c[0] for c in calls] == ['probe', 'install']


@pytest.mark.parametrize('cuda,mps,label', [
    (False, True, 'Apple GPU（MPS 可用）'),
    (False, False, 'CPU'),
    (True, False, 'CUDA'),
])
def test_ai_probe_reports_available_hardware_without_cuda_upgrade_warning(monkeypatch, tmp_path, cuda, mps, label):
    from krok_helper.audio_processing.separation import studio_ai
    from strange_uta_game.backend.application.ai_timing.runtime import AiRuntimeManager

    exe = tmp_path / 'overlay/bin/python3'
    exe.parent.mkdir(parents=True)
    exe.touch()
    (exe.parent.parent / 'lin-k-ready.json').write_text('{}')
    info = dict(torch='2.7.1', torchaudio='2.7.1', transformers='5.15.0',
                cuda=cuda, mps=mps, gpu_name='NVIDIA' if cuda else '')
    monkeypatch.setattr(studio_ai.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=json.dumps(info)))
    manager = AiRuntimeManager()
    studio_ai.configure_runtime(manager)
    status = manager.probe(str(exe))
    assert status.available and status.summary.endswith(label)
    assert status.cuda_available == cuda
    assert status.mps_available == mps
    assert not status.note
    if mps:
        assert not status.gpu_name  # SUG interprets this field as an NVIDIA GPU.


def test_editor_adapter_keeps_service_runtime_reference():
    from krok_helper.audio_processing.separation.studio_ai import install_editor_adapter
    from strange_uta_game.backend.application.ai_timing.runtime import AiRuntimeManager

    manager = AiRuntimeManager()
    service = SimpleNamespace(runtime=manager)
    parts = (service, None, None, manager)
    editor = SimpleNamespace(_build_ai_timing_service=lambda: parts)
    original_probe = manager.probe
    install_editor_adapter(editor)
    install_editor_adapter(editor)
    result = editor._build_ai_timing_service()
    assert result[0].runtime is manager
    assert manager.probe != original_probe
