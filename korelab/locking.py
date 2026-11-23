"""Exclusive cross-platform run locks."""
from __future__ import annotations

import errno
import os


def try_lock(file_obj) -> None:
    if os.name == "nt":
        import msvcrt
        file_obj.seek(0, os.SEEK_END)
        if file_obj.tell() == 0:
            file_obj.write(b"\0")
            file_obj.flush()
        file_obj.seek(0)
        try:
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                raise BlockingIOError from exc
            raise
    else:
        import fcntl
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
