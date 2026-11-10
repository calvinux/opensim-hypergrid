#!/usr/bin/env python3
"""Copy console output to stdout and keep only its last 100 lines on disk."""
import os
import stat
import sys
import tempfile
from collections import deque

MAX_LINES = 100


def save(path, lines):
    directory = os.path.dirname(os.path.abspath(path))
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, temporary = tempfile.mkstemp(prefix=".console-log-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(b"".join(lines))
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(path, initialize_only=False):
    try:
        with open(path, "rb") as existing:
            lines = deque(existing, maxlen=MAX_LINES)
    except FileNotFoundError:
        lines = deque(maxlen=MAX_LINES)
    save(path, lines)
    if initialize_only:
        return

    while True:
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        parts = chunk.split(b"\n")
        if lines and not lines[-1].endswith(b"\n"):
            parts[0] = lines.pop() + parts[0]
        for part in parts[:-1]:
            lines.append(part + b"\n")
        if parts[-1]:
            lines.append(parts[-1])
        save(path, lines)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--initialize"):
        sys.exit("usage: console_log.py PATH [--initialize]")
    run(sys.argv[1], len(sys.argv) == 3)
