import os
import threading

import pytest

from disktree.scanner import Cancelled, Scanner, human_size, list_dir, measure

MIB = 1024 * 1024


def make(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))      # real data, so blocks are really allocated


def test_human_size():
    assert human_size(None) == "…"
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(5 * 1024**3) == "5.0 GiB"


def test_measure_counts_nested_files_and_fills_cache(tmp_path):
    make(tmp_path / "a" / "one.bin", MIB)
    make(tmp_path / "a" / "b" / "two.bin", 2 * MIB)
    cache = {}
    total = measure(str(tmp_path), cache)
    assert total >= 3 * MIB
    assert cache[str(tmp_path / "a" / "b")] >= 2 * MIB
    assert cache[str(tmp_path)] == total
    assert cache[str(tmp_path / "a")] >= 3 * MIB


def test_symlinks_are_skipped(tmp_path):
    make(tmp_path / "real.bin", MIB)
    (tmp_path / "link.bin").symlink_to(tmp_path / "real.bin")
    cache = {}
    assert measure(str(tmp_path), cache) < 2 * MIB


def test_hardlinks_are_counted_once(tmp_path):
    make(tmp_path / "x.bin", MIB)
    os.link(tmp_path / "x.bin", tmp_path / "y.bin")
    assert measure(str(tmp_path), {}) < 2 * MIB


def test_cached_subfolders_are_reused(tmp_path):
    make(tmp_path / "sub" / "f.bin", MIB)
    cache = {str(tmp_path / "sub"): 123}
    assert measure(str(tmp_path), cache) == 123     # trusts the cache, does not descend


def test_cancel(tmp_path):
    make(tmp_path / "f.bin", 1000)
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        measure(str(tmp_path), {}, ev)


def test_list_dir_leaves_folder_sizes_unset(tmp_path):
    make(tmp_path / "d" / "f.bin", 10)
    make(tmp_path / "file.bin", MIB)
    rows = {r[0]: r for r in list_dir(str(tmp_path))}
    assert rows["d"][2] is True and rows["d"][3] is None
    assert rows["file.bin"][2] is False and rows["file.bin"][3] >= MIB


def test_scanner_reports_listing_sizes_then_done(tmp_path):
    make(tmp_path / "big" / "f.bin", 2 * MIB)
    make(tmp_path / "small" / "f.bin", 10)
    events, finished = [], threading.Event()
    s = Scanner({})
    s.scan(str(tmp_path),
           lambda p, entries, err: events.append(("listed", len(entries))),
           lambda p, child, size: events.append(("sized", os.path.basename(child), size)),
           lambda p: (events.append(("done",)), finished.set()))
    assert finished.wait(10)
    assert events[0] == ("listed", 2) and events[-1] == ("done",)
    sized = {e[1]: e[2] for e in events if e[0] == "sized"}
    assert sized["big"] >= 2 * MIB and set(sized) == {"big", "small"}
