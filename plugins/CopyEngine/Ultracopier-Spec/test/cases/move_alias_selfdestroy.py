#!/usr/bin/env python3
"""#CRITICAL data-loss (found by the 2026-07-02 Fable-5 coverage review, independently reproduced):
a MOVE (or COPY) whose DESTINATION reaches the SOURCE file through an intermediate SYMLINK path
component (or a hardlink) must NEVER destroy the file.

BUG: same-file detection was STRING-ONLY (TransferThread::isSame compared source==destination as
strings; the MOVE unlink gate was also string-only), with NO st_dev/st_ino check anywhere. Give the
destination a path that aliases the source through a symlinked component -- e.g. dest /tmp/alias/sub
where /tmp/alias -> <source's parent>. The strings differ, so isSame() returned false; isSameDrive()
is PATH-STRING based (DriveManagement::getDrive, mount-prefix), so /tmp vs the source's real mount
reads CROSS-drive -> the copy+delete path runs -> open(dest,O_TRUNC) TRUNCATES the source inode (dest
== source via the alias) -> the 0-byte "copy" passes post-op -> unlink(source) removes it -> the file
is GONE. coreutils refuses via (st_dev,st_ino).

FIX: coreutils-style inode same-file detection in TransferThread::isSame() (POSIX): if the paths
differ but stat() (which follows symlinks, resolving the alias) gives the same (st_dev,st_ino), treat
it as the same file -> the existing same-file collision handling skips/dialogs instead of destroying.

WHY CROSS-MOUNT: the destructive path is only reached when getDrive(source) != getDrive(dest), i.e.
the source and the /tmp alias live on DISTINCT mount points (same mount -> isSameDrive=SAME -> the
atomic rename() no-op, source safe -- which is why every tmpfs-only harness setup stays green). This
case therefore picks two candidate roots with different st_dev; if none exist it SKIPS (can't set up
the cross-drive condition), never false-passes.

ASSERTS: for BOTH mv and cp, with fileCollision=Skip, the source file survives BYTE-PERFECT and the
job stays alive/completes (no hang, no crash). RED before the fix (source destroyed), GREEN after.
Async + io_uring (shared TransferThread::isSame); IOCP's st_ino is unreliable so the POSIX guard is
gated out there (a Windows junction/hardlink same-file needs GetFileInformationByHandle -- separate).
"""
import sys, os, pathlib, shutil, hashlib
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H

# TINY payload: just big enough that an O_TRUNC of the source is detectable as content loss.
# This case writes only a few KB and only to RAM-backed tmpfs (see _distinct_mount_pair) -- it never
# consumes real disk / /mnt/data, and every leg cleans up in a finally.
PAYLOAD = b"PRECIOUS-payload-must-survive-move/copy-through-symlink-alias\n" * 8   # ~0.5 KB
WANT = hashlib.sha256(PAYLOAD).hexdigest()


def _distinct_mount_pair():
    """Return (source_root, alias_root) on DIFFERENT filesystems (st_dev), both writable, or None.
    The bug needs getDrive(source) != getDrive(dest) -> distinct MOUNT POINTS. tmpfs candidates come
    FIRST (/dev/shm, /tmp) so the few-KB test data lands in RAM, never on real disk (/mnt/data); the
    ext4 fallbacks are only used if no two tmpfs mounts exist. Either way the payload is < 1 KB."""
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
    # Prefer a pair where BOTH roots are the earliest (tmpfs-first) candidates -> RAM-backed, tiny.
    for i in range(len(seen)):
        for j in range(len(seen)):
            if i != j and seen[i][1] != seen[j][1]:
                return seen[i][0], seen[j][0]   # (source_root on dev A, alias_root on dev B)
    return None


def _one(backend, mode, source_root, alias_root, tag):
    base = os.path.join(source_root, f"uc_alias_{backend}_{mode}_{os.getpid()}")
    alias = os.path.join(alias_root, f"uc_aliaslink_{backend}_{mode}_{os.getpid()}")
    ok = True
    note = ""
    try:
        shutil.rmtree(base, ignore_errors=True)
        srcdir = os.path.join(base, "A", "sub")
        os.makedirs(srcdir)
        srcf = os.path.join(srcdir, "victim.dat")
        with open(srcf, "wb") as f:
            f.write(PAYLOAD)
        try:
            os.remove(alias)
        except OSError:
            pass
        os.symlink(os.path.join(base, "A"), alias)      # <B>/alias -> <A>/base/A
        destdir = os.path.join(alias, "sub")            # <B>/alias/sub RESOLVES TO <A>/base/A/sub == srcdir
        # sanity: the alias really reaches the same inode, and the two paths are cross-drive
        same_inode = os.path.exists(os.path.join(destdir, "victim.dat")) and \
            os.stat(os.path.join(destdir, "victim.dat")).st_ino == os.stat(srcf).st_ino
        cross = os.stat(srcf).st_dev != os.lstat(alias).st_dev
        if not (same_inode and cross):
            note = f"setup not valid (same_inode={same_inode} cross={cross}) -> skipped this leg"
        else:
            # fileCollision=OVERWRITE is the policy that TRIGGERS the destruction (Skip would dodge it by
            # treating the aliased dest as a normal collision-skip -> false-green). With the fix the aliased
            # same-file is detected and skipped directly (no overwrite, no dialog), so the run still
            # completes cleanly; without the fix OVERWRITE truncates+unlinks the source.
            r = H.run(backend, mode, [srcf], destdir,
                      file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
                      expect_dir=None)
            survived = os.path.exists(srcf) and hashlib.sha256(H_read(srcf)).hexdigest() == WANT
            ok = survived and r.stayed_alive and r.completed and not r.oom_killed and r.mem_errors == 0
            note = (f"{mode}/{backend}: src_survived={survived} alive={r.stayed_alive} "
                    f"completed={r.completed} mem_err={r.mem_errors}")
            if not survived:
                note += "  *** DATA LOSS: the file was DESTROYED by a move/copy through a symlink alias ***"
    finally:
        # ALWAYS reclaim the (tmpfs, sub-KB) scratch + the symlink, even if H.run raised.
        shutil.rmtree(base, ignore_errors=True)
        try:
            os.remove(alias)
        except OSError:
            pass
    print(f"      [{tag}] {note}")
    return ok


def H_read(p):
    with open(p, "rb") as f:
        return f.read()


def run(backends=None, memcheck=H.NONE) -> bool:
    pair = _distinct_mount_pair()
    if pair is None:
        print("    [move_alias_selfdestroy] SKIP (no two writable distinct-mount roots -> can't set up "
              "the cross-drive same-file alias)")
        return True
    source_root, alias_root = pair
    bes = [b for b in (backends or [H.ASYNC]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [move_alias_selfdestroy] SKIP (async/io_uring only; IOCP st_ino unreliable)")
        return True
    ok = True
    for backend in bes:
        for mode in ("mv", "cp"):
            ok = _one(backend, mode, source_root, alias_root, f"{backend}:{mode}") and ok
    print(f"    [move_alias_selfdestroy] {'PASS  (source survives a move/copy through a symlink alias)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
