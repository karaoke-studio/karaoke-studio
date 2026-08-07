from __future__ import annotations

from types import SimpleNamespace

from krok_helper.audio_processing.separation import integration


def test_cuda_runtime_requires_a_cuda_12_8_capable_windows_driver(monkeypatch) -> None:
    monkeypatch.setattr(integration.shutil, "which", lambda _name: "nvidia-smi.exe")
    monkeypatch.setattr(
        integration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="570.65\n"),
    )
    assert integration.nvidia_driver_available()

    monkeypatch.setattr(
        integration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="566.36\n"),
    )
    assert not integration.nvidia_driver_available()


def test_runtime_variant_can_be_forced_for_support_and_tests(monkeypatch) -> None:
    monkeypatch.setattr(integration.platform, "system", lambda: "Windows")
    monkeypatch.setenv("KROK_PYMSS_RUNTIME_VARIANT", "windows-cpu")
    assert integration.managed_runtime_variant() == "windows-cpu"
