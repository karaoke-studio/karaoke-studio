from krok_helper.qt_audio import follow_default_audio_output


class _Signal:
    def connect(self, slot):
        self.slot = slot

    def emit(self):
        self.slot()


class _Devices:
    def __init__(self, default):
        self.default = default
        self.audioOutputsChanged = _Signal()

    def defaultAudioOutput(self):
        return self.default


class _AudioOutput:
    def __init__(self):
        self.devices = []

    def setDevice(self, device):
        self.devices.append(device)


def test_follow_default_audio_output_sets_current_and_tracks_changes():
    output = _AudioOutput()
    devices = _Devices("speakers")

    retained = follow_default_audio_output(output, media_devices=devices)
    devices.default = "headphones"
    devices.audioOutputsChanged.emit()

    assert output.devices == ["speakers", "headphones"]
    assert retained is devices
    assert output._krok_media_devices is devices
