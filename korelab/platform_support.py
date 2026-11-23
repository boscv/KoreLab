"""Narrow platform-specific primitives used by the runtime."""
from __future__ import annotations

import os
import pathlib


def _private_sddl(sid: str, *, directory: bool) -> str:
    inheritance = ";OICI" if directory else ""
    return f"D:P(A{inheritance};FA;;;{sid})"


def set_private_permissions(path: pathlib.Path, mode: int) -> None:
    """Restrict a file or directory to its owner, failing closed on Windows."""
    path = pathlib.Path(path)
    if os.name != "nt":
        path.chmod(mode)
        return

    # Python's chmod on Windows controls only the read-only bit. Install a
    # protected DACL for the current token SID instead of claiming POSIX
    # 0600/0700 semantics that Windows does not provide.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]
    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    process = kernel.GetCurrentProcess()
    if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "could not open current Windows access token")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "could not read current Windows token identity")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents.User.Sid
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "could not format current Windows identity")
        try:
            sddl = _private_sddl(sid_text.value, directory=path.is_dir())
            descriptor = ctypes.c_void_p()
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(descriptor), None):
                raise OSError(ctypes.get_last_error(), "could not build private Windows access control list")
            try:
                dacl_offset = 0x00000004 | 0x80000000
                if not advapi.SetFileSecurityW(str(path), dacl_offset, descriptor):
                    raise OSError(ctypes.get_last_error(), "could not secure private Windows runtime path")
            finally:
                kernel.LocalFree(descriptor)
        finally:
            kernel.LocalFree(sid_text)
    finally:
        kernel.CloseHandle(token)
