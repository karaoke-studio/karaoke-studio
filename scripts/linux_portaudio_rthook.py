"""PyInstaller runtime hook (Linux): use the bundled PortAudio as a fallback.

sounddevice locates PortAudio with ``ctypes.util.find_library('portaudio')``
and, on Linux, gives up when the system has none. Source runs need
``libportaudio2`` from the distribution; the packaged app ships
``libportaudio.so.2`` and only uses it when the system library is missing,
so a distribution build (with its ALSA/JACK/Pulse plugins) still wins.
"""
import ctypes.util
import os
import sys

_bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "libportaudio.so.2")
if os.path.isfile(_bundled):
    _system_find_library = ctypes.util.find_library

    def _find_library(name):
        found = _system_find_library(name)
        if found is None and name == "portaudio":
            return _bundled
        return found

    ctypes.util.find_library = _find_library
