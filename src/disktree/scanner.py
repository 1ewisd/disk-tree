"""Directory size scanning.

Sizes are *allocated disk usage* (st_blocks * 512), like `du`, so sparse files (VM images, ...)
don't look bigger than they are and hard links are counted once. Symlinks are skipped and
other filesystems are not crossed. Every directory measured on the way is cached, so expanding
a folder you've already sized is instant.
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor, wait


class Cancelled(Exception):
    pass


def human_size(n):
    if n is None:
        return "…"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def measure(path, cache, cancel=None, seen=None):
    """Total allocated bytes under `path`. Fills `cache[dir]` for every directory beneath it."""
    seen = set() if seen is None else seen
    try:
        dev = os.stat(path).st_dev
    except OSError:
        cache[path] = 0
        return 0
    paths, parents, sizes = [], [], []
    stack = [(path, -1)]
    while stack:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        p, parent = stack.pop()
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
                            if st.st_dev == dev:
                                stack.append((e.path, idx))
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


def list_dir(path):
    """Direct children as [(name, path, is_dir, size_or_None, other_fs)]. Dir sizes are left None."""
    dev = os.stat(path).st_dev
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if e.is_dir(follow_symlinks=False):
                    out.append((e.name, e.path, True, None, st.st_dev != dev))
                else:
                    out.append((e.name, e.path, False, st.st_blocks * 512, False))
            except OSError:
                out.append((e.name, e.path, False, None, False))
    return out


class Scanner:
    """Lists a folder at once, then sizes its sub-folders in parallel and reports as they finish."""

    def __init__(self, cache, workers=6):
        self.cache = cache
        self.cancel = threading.Event()
        self.seen = set()
        self._list_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dt-list")
        self._size_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dt-size")

    def reset(self):
        """Cancel everything in flight and start with a fresh cancel flag."""
        self.cancel.set()
        self.cancel = threading.Event()
        self.seen = set()

    def stop(self):
        self.cancel.set()

    def scan(self, path, on_listed, on_sized, on_done):
        cancel, seen = self.cancel, self.seen

        def job():
            try:
                entries = list_dir(path)
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
                        size = measure(child, self.cache, cancel, seen)
                except Cancelled:
                    return
                on_sized(path, child, size)

            futs = [self._size_pool.submit(size_one, p) for _n, p, d, _s, other in entries if d and not other]
            wait(futs)
            if not cancel.is_set():
                on_done(path)

        self._list_pool.submit(job)
