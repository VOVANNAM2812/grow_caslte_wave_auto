"""Read local MP4 frames with Windows Media Foundation."""

import ctypes as ct
from pathlib import Path
from uuid import UUID

from PIL import Image


_DWORD = ct.c_uint32
_PTR = ct.c_void_p
_OUT_PTR = ct.POINTER(_PTR)
_VIDEO = 0xFFFFFFFC
_ALL_STREAMS = 0xFFFFFFFE
_END_OF_STREAM = 2


def _check(result):
    if result < 0:
        raise OSError(f"Media Foundation error 0x{result & 0xFFFFFFFF:08X}")


def _guid(value):
    return ct.byref((ct.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le))


def _method(obj, index, result, *types):
    table = ct.cast(obj, ct.POINTER(ct.POINTER(_PTR)))[0]
    return ct.WINFUNCTYPE(result, _PTR, *types)(table[index])


def _call(obj, index, types, *args):
    _check(_method(obj, index, ct.c_long, *types)(obj, *args))


def _release(obj):
    if obj.value:
        _method(obj, 2, _DWORD)(obj)


def read_video(path):
    """Yield (frame index, presentation time in seconds, RGB image)."""
    path = Path(path).resolve(strict=True)
    ole = ct.WinDLL("ole32")
    media = ct.WinDLL("mfplat")
    source = ct.WinDLL("mfreadwrite")
    source.MFCreateSourceReaderFromURL.argtypes = (ct.c_wchar_p, _PTR, _OUT_PTR)
    _check(ole.CoInitializeEx(None, 0))
    started = False
    attrs, reader, requested, current = (_PTR() for _ in range(4))
    try:
        _check(media.MFStartup(0x20070, 0))
        started = True
        _check(media.MFCreateAttributes(ct.byref(attrs), 1))
        # Enable conversion from the decoder's native YUV format to RGB32.
        _call(attrs, 21, (_PTR, _DWORD), _guid("0f81da2c-b537-4672-a8b2-a681b17307a3"), 1)
        _check(source.MFCreateSourceReaderFromURL(str(path), attrs, ct.byref(reader)))
        _call(reader, 4, (_DWORD, ct.c_int), _ALL_STREAMS, 0)
        _call(reader, 4, (_DWORD, ct.c_int), _VIDEO, 1)
        _check(media.MFCreateMediaType(ct.byref(requested)))
        _call(requested, 24, (_PTR, _PTR),
              _guid("48eba18e-f8c9-4687-bf11-0a74c9f96a8f"),
              _guid("73646976-0000-0010-8000-00aa00389b71"))
        _call(requested, 24, (_PTR, _PTR),
              _guid("f7e34c9a-42e8-4714-b74b-cb29d72c35e5"),
              _guid("00000016-0000-0010-8000-00aa00389b71"))
        _call(reader, 7, (_DWORD, _PTR, _PTR), _VIDEO, None, requested)
        _call(reader, 6, (_DWORD, _OUT_PTR), _VIDEO, ct.byref(current))
        size = ct.c_uint64()
        _call(current, 8, (_PTR, ct.POINTER(ct.c_uint64)),
              _guid("1652c33d-d6b2-4012-b834-72030849a37d"), ct.byref(size))
        width, height = size.value >> 32, size.value & 0xFFFFFFFF
        stride = _DWORD()
        try:
            _call(current, 7, (_PTR, ct.POINTER(_DWORD)),
                  _guid("644b4e48-1e02-4516-b0eb-c01ca9d49ac6"), ct.byref(stride))
            row_bytes = abs(ct.c_int32(stride.value).value)
        except OSError:
            row_bytes = width * 4

        read = _method(reader, 9, ct.c_long, _DWORD, _DWORD, ct.POINTER(_DWORD),
                       ct.POINTER(_DWORD), ct.POINTER(ct.c_int64), _OUT_PTR)
        frame = 0
        while True:
            flags, timestamp, sample = _DWORD(), ct.c_int64(), _PTR()
            _check(read(reader, _VIDEO, 0, None, ct.byref(flags), ct.byref(timestamp),
                        ct.byref(sample)))
            try:
                if flags.value & _END_OF_STREAM:
                    break
                if not sample.value:
                    continue
                buffer = _PTR()
                _call(sample, 41, (_OUT_PTR,), ct.byref(buffer))
                try:
                    address, length = _PTR(), _DWORD()
                    _call(buffer, 3, (_OUT_PTR, ct.POINTER(_DWORD), ct.POINTER(_DWORD)),
                          ct.byref(address), None, ct.byref(length))
                    try:
                        raw = ct.string_at(address, length.value)
                    finally:
                        _call(buffer, 4, ())
                    image = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX", row_bytes)
                finally:
                    _release(buffer)
            finally:
                _release(sample)
            yield frame, timestamp.value / 10_000_000, image
            frame += 1
    finally:
        for obj in (current, requested, reader, attrs):
            _release(obj)
        if started:
            media.MFShutdown()
        ole.CoUninitialize()
