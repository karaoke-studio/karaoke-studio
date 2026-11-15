"""Per-editor SUG adapter: Studio supplies separation, AI only needs alignment.

Keep this boundary local to the embedded editor until SUG exposes a runtime
factory. Do not change module globals or standalone/Windows dependencies.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def is_studio_overlay(executable: str) -> bool:
    return bool(executable and (Path(executable).resolve().parent.parent / 'lin-k-ready.json').is_file())


def configure_runtime(runtime) -> None:
    if getattr(runtime, '_lin_k_studio_configured', False):
        return
    from strange_uta_game.backend.application.ai_timing.runtime import (
        AiRuntimeError, PINNED_TRANSFORMERS, PIP_DEFAULT_INDEX, RuntimeStatus,
        detect_torch_build, torchaudio_pin, open_install_log, wrap_progress_with_log,
    )

    original_probe = runtime.probe
    original_install = runtime.install_shared

    def probe(python_exe='', timeout_s=30.0):
        if not is_studio_overlay(python_exe):
            return original_probe(python_exe, timeout_s=timeout_s)
        code = (
            'import json,torch,torchaudio,soundfile,transformers; '
            'from transformers import Wav2Vec2ForCTC,Wav2Vec2Processor; '
            'from torchaudio.functional import resample; '
            'print(json.dumps({"torch":torch.__version__,"torchaudio":torchaudio.__version__, '
            '"transformers":transformers.__version__}))'
        )
        try:
            result = subprocess.run([python_exe, '-c', code], capture_output=True,
                                    text=True, timeout=max(timeout_s, 90))
            if result.returncode:
                return RuntimeStatus(available=False, python_path=python_exe,
                                     message='对齐环境尚未就绪，请点击安装 / 修复。' + result.stderr[-500:])
            info = json.loads(result.stdout.strip().splitlines()[-1])
            return RuntimeStatus(available=True, python_path=python_exe,
                                 torch_version=info['torch'], torchaudio_version=info['torchaudio'],
                                 transformers_version=info['transformers'])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
            return RuntimeStatus(available=False, python_path=python_exe, message=f'对齐环境检测失败：{exc}')

    def install_shared(python_exe, *, mirror='', proxy='', progress=None, cancel=None, log=None):
        if not is_studio_overlay(python_exe):
            return original_install(python_exe, mirror=mirror, proxy=proxy,
                                    progress=progress, cancel=cancel, log=log)
        progress = progress or (lambda *_: None)
        cancel = cancel or (lambda: False)
        if cancel():
            raise AiRuntimeError('已取消')
        build = detect_torch_build(python_exe)
        if build is None:
            raise AiRuntimeError('Studio 底座已变化或 PyTorch 不可用，请重新关联 Studio。')
        if log is None:
            log = open_install_log(Path(python_exe).resolve().parent.parent)
        runtime._install_log = log
        progress = wrap_progress_with_log(progress, log)
        version, tag = build
        args = ['-m', 'pip', 'install', '--disable-pip-version-check',
                '--no-warn-script-location', '-i', mirror or PIP_DEFAULT_INDEX,
                torchaudio_pin(version, tag), PINNED_TRANSFORMERS,
                'soundfile', 'huggingface_hub', 'librosa==0.10.2.post1']
        if proxy:
            args += ['--proxy', proxy]
        progress(2, '复用 Studio 的 PyTorch，正在 Lin-K 环境安装 AI 对齐依赖…')
        return runtime._pip_install_requirements(python_exe, args, progress=progress, cancel=cancel)

    runtime.probe = probe
    runtime.install_shared = install_shared
    runtime._lin_k_studio_configured = True


def install_editor_adapter(editor) -> None:
    """Adapt each freshly built runtime in place, preserving service references."""
    if getattr(editor, '_lin_k_studio_ai_adapter', False):
        return
    original = editor._build_ai_timing_service

    def build():
        parts = original()
        if parts is not None:
            configure_runtime(parts[3])
        return parts

    editor._build_ai_timing_service = build
    editor._lin_k_studio_ai_adapter = True
