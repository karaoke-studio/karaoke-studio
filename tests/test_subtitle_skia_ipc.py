"""POSIX rings stay readable until acknowledgement, then release their names."""
import ctypes
import os
import sys
import time
import uuid

import pytest
from PyQt6.QtCore import QNativeIpcKey, QSharedMemory

from test_subtitle_skia_protocol import application, binary, sample, selection, collect
from krok_helper.subtitle_render.native.backend import SharedFrameRingReader
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_ipc import OwnedPosixRings, posix_ring_name

pytestmark=pytest.mark.skipif(sys.platform=='win32',reason='POSIX named-object lifetime')


def exists(key):
    name=posix_ring_name(key)
    assert name is not None
    libc=ctypes.CDLL(None,use_errno=True)
    libc.shm_open.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_uint]
    libc.shm_open.restype=ctypes.c_int
    fd=libc.shm_open(name.encode(),os.O_RDONLY,0)
    if fd<0: return False
    os.close(fd); return True


def flush(process):
    process._send({'cmd':'backend_info'})
    process._expect_ok(process._read_until_event('backend_info'))


def test_reader_can_attach_late_and_mapping_survives_release(binary):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*sample(),width=320,height=180,fps=60)
        started=process.start_render_range([400,600],generation=1,threads=1,
                                   shm_key='client-hint-'+uuid.uuid4().hex,ring_slots=2)
        events=[]
        while len(events)<2:
            event=process.read_event()
            if event['event']=='frame_ready': events.append(event)
        key=events[0]['shm_key']
        assert key==started['shm_key'] and exists(key)
        # Producer may already have completed; the client hasn't consumed its
        # range_done, so late readers can still attach to all retained slots.
        with SharedFrameRingReader.from_event(events[0]) as reader:
            before=reader.read_frame(events[0]).payload
            process._expect_ok(process._read_until_event('range_done'))
            flush(process)
            assert not exists(key)
            assert reader.read_frame(events[0]).payload==before


def test_repeated_ranges_do_not_accumulate_posix_objects(binary):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*sample(),width=320,height=180,fps=60)
        started=time.monotonic(); count=0
        # Exercise the actual growing-preview path for at least 12 seconds,
        # not merely a startup smoke that misses progressive exhaustion.
        while count<100 or time.monotonic()-started<12:
            frames,events=collect(process,[550],generation=count+1)
            assert frames[0].any()
            key=events[0]['shm_key']
            flush(process)
            assert not exists(key),(count,key)
            count+=1
        assert not process._posix_rings._keys


@pytest.mark.parametrize('stop',['close','abort','external_kill'])
def test_unacknowledged_range_is_reclaimed_when_sidecar_stops(binary,stop):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    key=None
    try:
        process.start(); process.configure(*sample(),width=320,height=180,fps=60)
        process.start_render_range([550],generation=1,threads=1,
                                   shm_key='unacknowledged-'+uuid.uuid4().hex,ring_slots=1)
        event=process._expect_ok(process._read_until_event('frame_ready'))
        key=event['shm_key']; assert exists(key)
        # Deliberately leave range_done unread; forced death cannot run native
        # destructors. The tracked private key is reclaimed by client close.
        if stop=='external_kill':
            process._process.kill(); process._process.wait(timeout=5); process.close()
        else: getattr(process,stop)()
        assert not exists(key)
    finally: process.close()


def test_release_cannot_unlink_an_unowned_name(binary):
    key=QNativeIpcKey('/ks-'+uuid.uuid4().hex[:24],QNativeIpcKey.Type.PosixRealtime)
    memory=QSharedMemory(key); assert memory.create(4096)
    encoded=key.toString()
    cleanup=OwnedPosixRings()
    cleanup.observe({'event':'range_started','shm_key':encoded},send=lambda _:None,is_running=lambda:True)
    try:
        with SkiaRendererProcess(binary,selection=selection('raster')) as process:
            process._send({'cmd':'release_frame_ring','shm_key':encoded})
            response=process._expect_ok(process._read_until_event('ring_released'))
            assert response['released'] is False and exists(encoded)
    finally:
        memory.detach(); cleanup.cleanup()

