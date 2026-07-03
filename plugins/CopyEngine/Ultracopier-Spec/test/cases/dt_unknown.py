#!/usr/bin/env python3
"""#HIGH (2026-07-02 coverage review, REPRODUCED): a source filesystem that reports every dirent's
d_type as DT_UNKNOWN -- sshfs and several network / FUSE mounts do -- makes the scan
(TransferThread::entryInfoList, POSIX branch) mis-classify a DIRECTORY as a file, because it set
isFolder = (d_type==DT_DIR) with no fallback. The directory is then never recursed and its WHOLE
SUBTREE is silently dropped from the backup -- a data-loss bug on a mainstream setup (copying FROM a
network share).

FIX: on DT_UNKNOWN, lstat the entry and classify by S_ISDIR. lstat (not stat) matches the raw-dirent
meaning -- a SYMLINK stays a symlink (copied as a link, never followed into recursion), only a REAL
directory recurses.

Injected with the LD_PRELOAD shim verb `dtunknown` (interposes readdir/readdir64, forces every
d_type to DT_UNKNOWN). Runs on async + io_uring: the SOURCE scan goes through libc opendir/readdir for
BOTH backends (only the io_uring DATA plane bypasses libc), so the shim faults both.

ASSERTS (per backend, under dtunknown):
  * the real nested subtree (subdir/mid.txt, subdir/deep/leaf.txt) IS copied byte-correct -- NOT
    dropped. RED before the fix (subtree missing); GREEN after.
  * a top-level file copies.
  * a symlink-to-directory and a symlink-to-file round-trip AS SYMLINKS (their target string is
    reproduced; the dir-symlink is NOT recursed / dereferenced) -- proves lstat, not stat.
  * job completes, stays alive, content diff (--no-dereference) clean, no mem errors.
"""
import sys, os, pathlib, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

FILES = {
    "top.txt": b"top level file\n",
    "subdir/mid.txt": b"inside the subdir\n",
    "subdir/deep/leaf.txt": b"deeply nested leaf\n",
}
LINKS = {                    # name -> target (relative); must round-trip as a symlink, not be followed
    "link_to_subdir": "subdir",
    "link_to_top": "top.txt",
}


def _build(root):
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "subdir", "deep"))
    for rel, data in FILES.items():
        with open(os.path.join(root, rel), "wb") as f:
            f.write(data)
    for name, target in LINKS.items():
        os.symlink(target, os.path.join(root, name))


def _one(backend):
    src = os.path.join("/dev/shm", f"dtunk_src_{backend}_{os.getpid()}")
    dest = os.path.join("/dev/shm", f"dtunk_dst_{backend}_{os.getpid()}")
    for p in (src, dest):
        shutil.rmtree(p, ignore_errors=True)
    os.makedirs(dest)
    _build(src)
    ok = True
    try:
        K.with_scenario("dtunknown")
        try:
            r = H.run(backend, "cp", [src], dest, fs_preload=K.fs_so(),
                      file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
                      expect_dir=None, inode_threads=4, mem_limit_mb=1024)
        finally:
            K.with_scenario("")
        copied = os.path.join(dest, os.path.basename(src))

        if not (r.stayed_alive and r.completed and r.mem_errors == 0):
            print(f"      [{backend}] run NOT ok: alive={r.stayed_alive} completed={r.completed} "
                  f"mem_errors={r.mem_errors}")
            ok = False
        # the real nested subtree must survive (the bug dropped it)
        for rel, data in FILES.items():
            p = os.path.join(copied, rel)
            if not (os.path.exists(p) and open(p, "rb").read() == data):
                print(f"      [{backend}] DROPPED/wrong (subtree data-loss): {rel}")
                ok = False
        # symlinks must round-trip AS SYMLINKS (lstat, not stat -> not followed/recursed)
        for name, target in LINKS.items():
            p = os.path.join(copied, name)
            if not os.path.islink(p):
                print(f"      [{backend}] symlink not preserved as a link (followed?): {name}")
                ok = False
            elif os.readlink(p) != target:
                print(f"      [{backend}] symlink target wrong: {name} -> {os.readlink(p)!r} (want {target!r})")
                ok = False
        # a dir-symlink must NOT have been recursed into (no copied/link_to_subdir/mid.txt)
        recursed = os.path.join(copied, "link_to_subdir", "mid.txt")
        if os.path.exists(recursed) and not os.path.islink(os.path.join(copied, "link_to_subdir")):
            print(f"      [{backend}] dir-symlink was DEREFERENCED + recursed (stat instead of lstat)")
            ok = False
        print(f"      [{backend}] alive={r.stayed_alive} completed={r.completed} mem_err={r.mem_errors}"
              f"  {'subtree kept + symlinks intact' if ok else 'FAIL'}")
    finally:
        for p in (src, dest):
            shutil.rmtree(p, ignore_errors=True)
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    bes = [b for b in (backends or [H.ASYNC, H.IO_URING]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [dt_unknown] SKIP (async/io_uring only; Windows uses FindFirstFile dwFileAttributes)")
        return True
    ok = True
    for b in bes:
        ok = _one(b) and ok
    print(f"    [dt_unknown] {'PASS  (DT_UNKNOWN dir recursed, subtree kept; symlinks not followed)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
