"""Windows DPAPI protection for the current user's Gemini key."""

import base64
import ctypes
from ctypes import wintypes

from .errors import ConfigError


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(value: bytes, protect: bool) -> bytes:
    source = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming = _Blob(len(value), source)
    outgoing = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ConfigError("Windows の資格情報保護に失敗しました。設定は変更されていません。")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(outgoing.pbData)


def protect_key(key: str) -> str:
    return base64.b64encode(_transform(key.encode("utf-8"), True)).decode("ascii")


def unprotect_key(encoded: str) -> str:
    try:
        return _transform(base64.b64decode(encoded, validate=True), False).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("保存された API キーを復号できません。設定ファイルを確認してください。") from exc
