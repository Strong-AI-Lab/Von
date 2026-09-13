"""One host-wide admission slot shared by prepared Unix accounts.

The operator creates a stable, read-only-to-consumers lock inode. All enrolled
workers, including the existing worker, must use it. The lock is retained by
coding subprocesses so controller interruption cannot admit another launch.
This intentionally serialises coding work; it is not a fleet scheduler.
"""

import fcntl
import os
import platform
import re
import subprocess
import stat
from contextlib import contextmanager
from pathlib import Path


def available_memory_bytes():
    if platform.system() == "Darwin":
        output = subprocess.check_output(["/usr/bin/vm_stat"], text=True)
        page_size = int(re.search(r"page size of (\d+) bytes", output).group(1))
        pages = sum(
            int(re.search(rf"{name}:\s+(\d+)", output).group(1))
            for name in ("Pages free", "Pages inactive", "Pages speculative")
        )
        return pages * page_size
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Cannot establish host available memory")


@contextmanager
def admission(config):
    binding = config.get("host_capacity")
    if not binding:
        yield ()
        return
    path = Path(binding["lock_path"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise PermissionError(
                "Host capacity lock must have a stable operator-owned inode"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        reserve = int(binding["reserve_bytes"])
        launch = int(binding["launch_headroom_bytes"])
        if reserve < 0 or launch <= 0:
            raise ValueError("Invalid host capacity memory budget")
        if available_memory_bytes() < reserve + launch:
            yield None
            return
        yield (fd,)
    finally:
        # Do not explicitly LOCK_UN: an inherited descriptor must retain the
        # lock if the controller dies while its coding child is still running.
        os.close(fd)
