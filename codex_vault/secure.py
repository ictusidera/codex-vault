"""Current-user Windows DPAPI; no third party Python packages."""
import base64
import ctypes
import csv
import os
from pathlib import Path
import subprocess

from .core import VaultError


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    sid = next(csv.reader(result.stdout.splitlines()))[1]
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                    f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   capture_output=True, check=True)


def dpapi(data, decrypt=False):
    if os.name != "nt":
        raise VaultError("DPAPIはWindows専用です。RESTIC_PASSWORD_FILEを利用してください")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, dest = Blob(len(data), buffer), Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), "Codex Vault restic password", None, None, None, 1, ctypes.byref(dest))
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise VaultError(f"DPAPI処理に失敗しました（同じWindowsユーザーで実行してください）: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def save_password(path, password):
    private_dir(Path(path).parent)
    Path(path).write_bytes(base64.b64encode(dpapi(password.encode("utf-8"))))


def load_password(path):
    return dpapi(base64.b64decode(Path(path).read_bytes()), decrypt=True).decode("utf-8")
