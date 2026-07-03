#!/usr/bin/env python3
"""#HIGH data-integrity (2026-07-02 coverage review, reproduced): a CROSS-FILESYSTEM MOVE of a tree
containing symlinks must complete MOVE semantics -- recreate each link verbatim at the destination
(readlink+symlink, never follow, dangling links stay dangling) AND remove the SOURCE link, so the
source tree ends EMPTY and is rmdir'd bottom-up.

BUG: the symlink-recreation branch (async TransferThreadAsync ~675, uring TransferThreadUring ~729)
sets realMove=true after readlink+symlink at the dest; the completion's source-unlink is gated by
`mode==Move && !realMove`, so realMove=true SKIPS unlinking the source link. Result: every symlink is
COPIED not moved -- the source links (and therefore the whole source directory) are LEFT BEHIND while
the job reports completed. move.py/move_crossfs_clean.py use the 'default' synthtree profile which has
NO symlinks, so this shipped green. (Same-drive symlink move uses rename() and is fine; only cross-fs
hits the recreate path.)

RIG: source on one tmpfs mount, dest on ANOTHER (distinct st_dev -> isSameDrive reads cross-drive ->
the recreate path). Tree = a regular file + relative/absolute/DANGLING/dir symlinks + a nested file.

ASSERTS (async + io_uring): dest matches a pristine reference by `diff -rq --no-dereference` (targets
verbatim, dangling stays dangling); the SOURCE tree is fully GONE (the move removed every link + the
dirs). RED before the fix (source symlinks + dir stranded); GREEN after.
"""
import sys, os, pathlib, shutil, subprocess
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H


def _distinct_mount_pair():
    cands = ["/dev/shm", "/tmp", "/var/tmp", os.path.expanduser("~"),
             H.load_config().get("paths", "DESTINATIONLINUX", fallback="/tmp/ultracopier-spec-test")]
    seen = []
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            if os.access(c, os.W_OK):
                seen.append((c, os.stat(c).st_dev))
        except OSError:
            pass
    for i in range(len(seen)):
        for j in range(len(seen)):
            if i != j and seen[i][1] != seen[j][1]:
                return seen[i][0], seen[j][0]
    return None


def _make_tree(root):
    """A small tree with every symlink flavor; returns the tree dir (to be moved)."""
    t = os.path.join(root, "tree")
    os.makedirs(os.path.join(t, "sub"))
    with open(os.path.join(t, "regular.txt"), "wb") as f:
        f.write(b"regular-file-content\n")
    with open(os.path.join(t, "sub", "nested.txt"), "wb") as f:
        f.write(b"nested\n")
    os.symlink("regular.txt", os.path.join(t, "link_rel"))          # relative -> sibling
    os.symlink("/etc/hostname", os.path.join(t, "link_abs"))        # absolute (escaping)
    os.symlink("does_not_exist_xyz", os.path.join(t, "link_dangling"))  # DANGLING
    os.symlink("sub", os.path.join(t, "link_dir"))                  # -> a directory
    return t


def _one(backend, source_root, alias_root):
    base = os.path.join(source_root, f"mvsym_{backend}_{os.getpid()}")
    ref = os.path.join(alias_root, f"mvsym_ref_{backend}_{os.getpid()}")   # pristine reference (copy of source)
    dst = os.path.join(alias_root, f"mvsym_dst_{backend}_{os.getpid()}")
    for p in (base, ref, dst):
        shutil.rmtree(p, ignore_errors=True)
    ok = True
    note = ""
    try:
        tree = _make_tree(base)
        os.makedirs(ref)
        shutil.copytree(tree, os.path.join(ref, "tree"), symlinks=True)   # exact reference of what dest must become
        os.makedirs(dst)
        r = H.run(backend, "mv", [tree], dst,
                  file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
                  expect_dir=None)
        # dest must equal the reference (links verbatim, dangling stays dangling)
        copied = os.path.join(dst, "tree")
        diff = subprocess.run(["diff", "-rq", "--no-dereference", os.path.join(ref, "tree"), copied],
                              capture_output=True, text=True)
        dest_ok = diff.returncode == 0
        # source must be GONE (the move removed every link AND the dirs)
        src_gone = not os.path.exists(tree)
        stranded = []
        if not src_gone:
            for dp, dirs, files in os.walk(tree):
                for n in files + dirs:
                    stranded.append(os.path.join(dp, n))
        ok = r.stayed_alive and r.completed and not r.oom_killed and r.mem_errors == 0 and dest_ok and src_gone
        note = (f"{backend}: alive={r.stayed_alive} completed={r.completed} dest_matches={dest_ok} "
                f"source_removed={src_gone} mem_err={r.mem_errors}")
        if not dest_ok:
            note += f"  dest-diff: {diff.stdout.strip()[:200]}"
        if not src_gone:
            note += f"  *** MOVE STRANDED SOURCE (degraded to copy): {stranded[:6]} ***"
    finally:
        for p in (base, ref, dst):
            shutil.rmtree(p, ignore_errors=True)
    print(f"      [{backend}] {note}")
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    pair = _distinct_mount_pair()
    if pair is None:
        print("    [move_symlinks_crossfs] SKIP (no two distinct-mount roots for a cross-fs move)")
        return True
    source_root, alias_root = pair
    bes = [b for b in (backends or [H.ASYNC]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [move_symlinks_crossfs] SKIP (async/io_uring only)")
        return True
    ok = all(_one(b, source_root, alias_root) for b in bes)
    print(f"    [move_symlinks_crossfs] {'PASS  (cross-fs symlink move: links recreated + source removed)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
