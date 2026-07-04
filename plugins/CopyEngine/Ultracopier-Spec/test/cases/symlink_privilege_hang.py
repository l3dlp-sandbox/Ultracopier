#!/usr/bin/env python3
"""End-of-copy HANG when symlink creation permanently fails (Windows 1314 repro).

REAL BUG (seen on the Win10 IOCP box copying the linux-4.4 tree): the kernel source
has many symlinks (e.g. arch/*/boot/dts/include/dt-bindings). On a Windows box without
SeCreateSymbolicLinkPrivilege, every CreateSymbolicLink fails with error 1314
(ERROR_PRIVILEGE_NOT_HELD). With the fileError policy = "put to end", each failing
symlink is deferred and retried; being un-creatable it never succeeds, so it stays the
LAST live transfer. When the last such entry is finally given up, the transfer list
drains through the put-to-end / skip path (ListThread::transferInodeIsClosed's
putAtBottomAfterError branch) -- which, unlike the normal completion branch, NEVER
merges actionToDoListInode_afterTheTransfer back into the active inode list. Every
non-empty directory's deferred SyncDate (folder-date finalization) is therefore
STRANDED forever, and safetyReschedule() doesn't inspect afterTheTransfer so it never
repairs it. Symptom: all file bytes copied, only directories left, 0 B/s, ETA infinity,
CPU ~0%, GUI alive -- a permanent hang at the tail. (52134/52209, "dt-bindings, 0B".)

We reproduce on Linux by making symlink() fail EPERM (the POSIX analog of 1314) via the
LD_PRELOAD shim verb `symlinkfail:<substr>`. This is NOT io_uring/async specific: symlink
recreation goes through libc symlink() on every backend, so the shim faults all of them.

REQUIRED behaviour (what a correct engine must do): copy every regular file; each un-creatable symlink
is deferred ONE pass by "put to end", retried once, and -- still failing -- escalated to the Ask dialog
(one deferred retry then ask; NEVER a 16x retry storm). Here the scripted dialog answers SKIP, so the
symlinks are skipped and the whole job STILL COMPLETES, INCLUDING the folder-date finalization (SyncDate)
tail. Two fixes make this hold and are BOTH exercised here:
  1. shared ListThread: transferInodeIsClosed's put-to-end drain merges actionToDoListInode_afterThe
     Transfer back when it empties the transfer list (same merge the normal path does), and
     safetyReschedule() rescues a stranded afterTheTransfer -- so deferred folder dates always finalize.
  2. pipelined skip(): its realMove branch stores transfer_stat=Idle BEFORE emitting the queued
     postOperationStopped, so ListThread's transferInodeIsClosed can't consume the one-shot completion
     while the stat still reads Transfer and drop it (the io_uring/IOCP skip-after-put-to-end strand).
Without either fix the copy hangs forever at the SyncDate tail (the reported 52134/52209 / "dt-bindings,
0B" bug). Several symlinks are used so multiple escalate+skip cycles run and the LAST live transfer is a
skipped symlink (the exact tail that maximally exposed the skip() ordering race).

ASSERTS (per backend):
  * every regular file is copied byte-perfect
  * EVERY destination directory carries the source mtime (981173106) -- proof the SyncDate
    folder-finalization tail actually RAN, i.e. the engine truly completed instead of
    stranding the tail. THIS is the assertion that fails on the bug (dirs keep their fresh
    mkdir mtime because the merge never happened).
  * stayed_alive and no mem errors
"""
import sys, pathlib, os, filecmp
_TEST_DIR = pathlib.Path(__file__).resolve().parents[1]
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(_TEST_DIR))
from lib import harness as H
from lib import casekit as K

OLD = 981173106   # 2001-02-03 04:05:06 UTC -- the make_meta_tree stamp convention


def _build_tree(root: str):
    """Non-empty nested dirs (each SyncDate defers to the tail) + ONE symlink that will fail to
    be created (the LAST live transfer once the files finish). Returns (files{rel}, dirs[rel], links[rel])."""
    import shutil as _sh
    if os.path.exists(root):
        _sh.rmtree(root)
    dirs = ["a", "a/b", "c"]
    for d in dirs:
        os.makedirs(os.path.join(root, d), exist_ok=True)
    files = {"a/f1.dat": b"one" + b"Z" * 300,
             "a/b/f2.dat": b"two" + b"Z" * 300,
             "c/f3.dat": b"three" + b"Z" * 300}
    for rel, data in files.items():
        with open(os.path.join(root, rel), "wb") as fh:
            fh.write(data)
    # Several un-creatable symlinks (names carry "lnk_" so the shim matches+fails them). Each is
    # deferred one pass, retried once, then escalated to Ask->Skip; they outlive the regular files so
    # the LAST live transfer is a skipped symlink -- the tail that exposed the skip() ordering race.
    # Cheap now (one retry each, not a 16x storm), so the harness never idle-kills mid-run.
    links = {"a/lnk_a": "f1.dat", "a/b/lnk_b": "f2.dat", "c/lnk_c": "f3.dat", "lnk_top": "a/f1.dat"}
    for rel, target in links.items():
        lp = os.path.join(root, rel)
        if os.path.lexists(lp):
            os.unlink(lp)
        os.symlink(target, lp)
    # old mtime on every file + dir (deepest first so the dir stamps stick)
    for rel in list(files) + ["a/b", "a", "c", "."]:
        try:
            os.utime(os.path.join(root, rel), (OLD, OLD), follow_symlinks=True)
        except OSError:
            pass
    return files, dirs, links


def _dirs_finalized(copied_root: str, dirs) -> tuple[bool, str]:
    """Every destination directory must carry the source mtime -> the SyncDate tail ran."""
    msgs = []
    ok = True
    for rel in ["."] + dirs:
        dp = os.path.join(copied_root, rel)
        if not os.path.isdir(dp):
            ok = False; msgs.append(f"missing dest dir: {rel}"); continue
        mt = int(os.stat(dp).st_mtime)
        if mt != OLD:
            ok = False
            msgs.append(f"dir NOT finalized (SyncDate stranded -> HANG): {rel} "
                        f"mtime={mt} != {OLD}")
    return ok, "\n".join(msgs)


def _files_ok(src_root: str, copied_root: str, files) -> tuple[bool, str]:
    msgs = []; ok = True
    for rel in files:
        sp = os.path.join(src_root, rel); dp = os.path.join(copied_root, rel)
        if not os.path.exists(dp):
            ok = False; msgs.append(f"missing file: {rel}")
        elif not filecmp.cmp(sp, dp, shallow=False):
            ok = False; msgs.append(f"file differs: {rel}")
    return ok, "\n".join(msgs)


def _one(backend, memcheck) -> bool:
    src_root = K.fresh_src_root("symlink_src")
    files, dirs, links = _build_tree(src_root)
    base = os.path.basename(src_root)
    dest = K.fresh_dest("symlink_dest")
    copied = os.path.join(dest, base)

    K.with_scenario("symlinkfail:lnk_")
    # put-to-end defers each symlink one pass, retries once, then escalates to the Ask dialog; the
    # scripted answer is SKIP -> the symlinks are skipped and the job completes (folder dates finalized).
    os.environ["ULTRACOPIER_TEST_FILE_ERROR_ACTION"] = "skip"
    try:
        r = H.run(backend, "cp", [src_root], dest,
                  file_collision=H.FileCollision.OVERWRITE,
                  folder_collision=H.FolderCollision.MERGE,
                  file_error=H.FileError.PUT_TO_END,
                  folder_error=H.FolderError.SKIP,
                  keep_date=True,
                  expect_dir=None,               # the symlink is intentionally absent; check by hand
                  fs_preload=K.fs_so(),
                  memcheck=memcheck)
    finally:
        K.with_scenario("")
        os.environ.pop("ULTRACOPIER_TEST_FILE_ERROR_ACTION", None)

    files_ok, fmsg = _files_ok(src_root, copied, files)
    dirs_ok, dmsg = _dirs_finalized(copied, dirs)
    ok = (r.stayed_alive and files_ok and dirs_ok
          and not r.oom_killed and r.mem_errors == 0)
    if not ok:
        print(f"      not ok [{backend}]: completed={r.completed} alive={r.stayed_alive} "
              f"files_ok={files_ok} dirs_finalized={dirs_ok} oom={r.oom_killed} "
              f"mem_errors={r.mem_errors}")
        if fmsg:
            print(f"        FILES: {fmsg}")
        if dmsg:
            print(f"        DIRS:  {dmsg}")
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    def one(backend):
        res = _one(backend, memcheck)
        print(f"    [{backend} symlink-privilege put-to-end] {'PASS' if res else 'FAIL'}")
        return res
    return K.for_backends(backends, one)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
