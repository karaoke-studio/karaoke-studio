"""Qt Multimedia helpers shared by the workbench playback surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PyQt6.QtMultimedia import QAudioOutput


def follow_default_audio_output(
    audio_output: "QAudioOutput",
    *,
    media_devices: Any | None = None,
) -> Any:
    """Keep an audio output attached to the current system default device."""
    if media_devices is None:
        from PyQt6.QtMultimedia import QMediaDevices

        devices = QMediaDevices(audio_output)
    else:
        devices = media_devices
    audio_output.setDevice(devices.defaultAudioOutput())
    devices.audioOutputsChanged.connect(
        lambda: audio_output.setDevice(devices.defaultAudioOutput())
    )
    # Retain the signal owner explicitly (and support non-QObject test doubles).
    audio_output._krok_media_devices = devices
    return devices
