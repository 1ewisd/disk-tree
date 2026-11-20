"""Directory size scanning.

Sizes are *allocated disk usage* (st_blocks * 512), like `du`, so sparse files (VM images, ...)
don't look bigger than they are and hard links are counted once. Symlinks are skipped. Real mounts
(/home, /boot, /tmp...) are measured; virtual ones (/proc, /sys...) and unmounted volumes such as
btrfs snapshots are not. Every directory measured on the way is cached, so expanding a folder
you've already sized is instant.
"""
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait


class Cancelled(Exception):
    pass


# Filesystems that hold no real data (or can hang a scan): never descended into.
VIRTUAL_FS = {"proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "debugfs",
              "tracefs", "pstore", "bpf", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs",
              "binfmt_misc", "efivarfs", "rpc_pipefs", "nsfs"}
REMOTE_PREFIXES = ("fuse.", "nfs", "cifs", "smb", "sshfs", "9p")


class Mounts:
    """Mount points from /proc/mounts: real ones are measured, virtual / remote ones are skipped.

    A directory on a different device that is *not* a mount point (a btrfs snapshot, for example)
    is skipped too: it usually shares its data with another volume, so counting it would double up.
    """

    def __init__(self):
        self.real, self.skip = set(), {}
        try:
            with open("/proc/mounts") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
            fstype = parts[2]
            if fstype in VIRTUAL_FS:
                self.skip[mp] = "virtual filesystem"
            elif fstype.startswith(REMOTE_PREFIXES):
                self.skip[mp] = "network / FUSE mount"
            else:
                self.real.add(mp)

    def skip_reason(self, path, child_dev, parent_dev):
        """Why `path` (a directory) should not be measured, or None."""
        if path in self.skip:
            return self.skip[path]
        if child_dev != parent_dev and path not in self.real:
            return "other volume"
        return None


def human_size(n):
    if n is None:
        return "…"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def measure(path, cache, cancel=None, seen=None, mounts=None):
    """Total allocated bytes under `path`. Fills `cache[dir]` for every directory beneath it."""
    seen = set() if seen is None else seen
    mounts = Mounts() if mounts is None else mounts
    try:
        dev0 = os.stat(path).st_dev
    except OSError:
        cache[path] = 0
        return 0
    paths, parents, sizes = [], [], []
    stack = [(path, -1, dev0)]
    while stack:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        p, parent, dev = stack.pop()
        if p != path and p in cache:                 # already measured: reuse, don't descend
            if parent >= 0:
                sizes[parent] += cache[p]
            continue
        idx = len(paths)
        paths.append(p)
        parents.append(parent)
        sizes.append(0)
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        st = e.stat(follow_symlinks=False)
                        if e.is_dir(follow_symlinks=False):
                            if mounts.skip_reason(e.path, st.st_dev, dev) is None:
                                stack.append((e.path, idx, st.st_dev))
                        else:
                            if st.st_nlink > 1:
                                key = (st.st_dev, st.st_ino)
                                if key in seen:
                                    continue
                                seen.add(key)
                            sizes[idx] += st.st_blocks * 512
                    except OSError:
                        pass
        except OSError:
            pass
    for i in range(len(paths) - 1, 0, -1):           # children always come after their parent
        sizes[parents[i]] += sizes[i]
    for p, s in zip(paths, sizes):
        cache[p] = s
    return sizes[0]


def list_dir(path, mounts=None):
    """Direct children as [(name, path, is_dir, size_or_None, skipped_reason_or_None)].

    Folder sizes are left None (they're measured separately)."""
    mounts = Mounts() if mounts is None else mounts
    dev = os.stat(path).st_dev
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if e.is_dir(follow_symlinks=False):
                    out.append((e.name, e.path, True, None, mounts.skip_reason(e.path, st.st_dev, dev)))
                else:
                    out.append((e.name, e.path, False, st.st_blocks * 512, None))
            except OSError:
                out.append((e.name, e.path, False, None, None))
    return out


class Scanner:
    """Lists a folder at once, then sizes its sub-folders in parallel and reports as they finish."""

    def __init__(self, cache, workers=6):
        self.cache = cache
        self.cancel = threading.Event()
        self.seen = set()
        self.mounts = Mounts()
        self._list_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dt-list")
        self._size_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dt-size")

    def reset(self):
        """Cancel everything in flight and start with a fresh cancel flag."""
        self.cancel.set()
        self.cancel = threading.Event()
        self.seen = set()
        self.mounts = Mounts()

    def stop(self):
        self.cancel.set()

    def scan(self, path, on_listed, on_sized, on_done):
        cancel, seen, mounts = self.cancel, self.seen, self.mounts

        def job():
            try:
                entries = list_dir(path, mounts)
            except OSError as exc:
                if not cancel.is_set():
                    on_listed(path, None, str(exc))
                    on_done(path)
                return
            if cancel.is_set():
                return
            on_listed(path, entries, None)

            def size_one(child):
                try:
                    if child in self.cache:
                        size = self.cache[child]
                    else:
                        size = measure(child, self.cache, cancel, seen, mounts)
                except Cancelled:
                    return
                on_sized(path, child, size)

            futs = [self._size_pool.submit(size_one, p) for _n, p, d, _s, skipped in entries if d and not skipped]
            wait(futs)
            if not cancel.is_set():
                on_done(path)

        self._list_pool.submit(job)
