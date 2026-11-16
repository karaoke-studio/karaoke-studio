"""Skia-only POSIX ring acknowledgements and dead-sidecar cleanup."""
import ctypes
import os
import re
import threading
from PyQt6.QtCore import QNativeIpcKey


def posix_ring_name(key):
    if os.name == 'nt' or not isinstance(key, str) or not key.startswith('posix:'):
        return None
    native = QNativeIpcKey.fromString(key)
    name = native.nativeKey()
    return name if (native.type() == QNativeIpcKey.Type.PosixRealtime
                    and re.fullmatch(r'/ks-[0-9a-f]{24}', name)) else None


class OwnedPosixRings:
    def __init__(self):
        self._keys = set()
        self._releasing = set()
        self._lock = threading.Lock()

    def observe(self, event, *, send, is_running):
        key = event.get('shm_key')
        if posix_ring_name(key) is None:
            return False
        kind = event.get('event')
        if kind in {'range_started', 'frame_ready'}:
            with self._lock: self._keys.add(key)
            if not is_running(): self.cleanup()
        elif kind == 'range_done':
            with self._lock: owned = key in self._keys
            if owned and is_running():
                with self._lock: self._releasing.add(key)
                try: send({'cmd': 'release_frame_ring', 'shm_key': key})
                except (OSError, RuntimeError): pass  # close/abort still owns cleanup.
        elif kind == 'ring_released':
            with self._lock:
                if key in self._releasing:
                    self._releasing.discard(key)
                    if event.get('released'): self._keys.discard(key)
                    return True
        return False

    def cleanup(self):
        with self._lock:
            if not self._keys: return
            libc = ctypes.CDLL(None, use_errno=True)
            for function in (libc.shm_unlink, libc.sem_unlink):
                function.argtypes = [ctypes.c_char_p]
                function.restype = ctypes.c_int
                for key in self._keys:
                    function(posix_ring_name(key).encode('ascii'))
            self._keys.clear()
            self._releasing.clear()
