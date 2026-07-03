#!/usr/bin/env python3
"""#HIGH (2026-07-02 coverage review, REPRODUCED): a HEALTHY copy of many SMALL files spuriously
trips ListThread::safetyReschedule()'s stall backstop, which then re-pumps LIVE entries and
double-starts them onto idle threads -> the SAME dest file gets create-opened on several threads.

ROOT CAUSE: safetyReschedule() gates "is a transfer running?" on getNumberOfTranferRuning(), which
counts only Transfer-state threads whose file is >= parallelizeIfSmallerThan (128 KB) -- that filter
exists for the big-file MAXPARALLELTRANFER cap. A backlog of files all < 128 KB therefore reads
"0 running" while copying perfectly fine; after ~1.5 s the backstop concludes "stalled" and runs its
full reset -- WHILE the healthy copy's per-file completions are still in flight. The reset's
`for(idle threads) transferId=0` then zeroes an Idle thread's id BEFORE that thread's queued
transferInodeIsClosed slot runs; that completion (indexOfActionToDoTransfer(transferId) -> tombstone
entry + clear id) now looks up id 0, cannot find its entry, and never tombstones it -> the entry
lingers isRunning/unremoved with no owner -> a later tick re-queues it -> doNewActions hands the SAME
file to a second thread and copies it AGAIN. Observed on the shipping async binary: hundreds of dest
files create-opened 2-5x per run (content survived only because the redundant copies write identical
bytes -- but it is a data race + massive wasted I/O).

FIX (ListThread::safetyReschedule body; the gate is UNCHANGED because its frequent firing is what
recovers a missed put-to-end pump): before touching anything, snapshot liveEntryIds (ids with a
non-tombstone entry) and ownedTransferIds (ids any thread still holds). Then zero an Idle thread's
stale id ONLY if no live entry claims it (a live entry means its completion is merely queued and will
clean up itself -- don't orphan it), and re-queue (isRunning=false) ONLY entries whose id no thread
owns (genuinely stranded). On a REAL stall every completion has already run, so both sets are empty and
the behaviour is identical to before -- the put-to-end retry recovery it was built for is preserved.

ASSERTS:
 * async  -- via the LD_PRELOAD op trace: NO duplicate create-open (and no overlapping write) of any
             dest file. RED before the fix (hundreds of dup create-opens); GREEN after (0). Plus the
             content diff and liveness.
 * io_uring -- the ring's open/read/write bypass libc so the op trace can't see them (and the `slow`
             shim can't stretch the ring data plane); this is a shared-code fix (ListThread), so the
             io_uring lane guards that the change did not regress it: the copy completes, stays alive,
             content matches, no mem errors.

The copy must last past the ~1.5 s stall window for the buggy backstop to fire, so it uses many small
files + a small per-op read/write delay (`slow`) and 16 inode threads (maximises the idle-cycling that
lets a re-pump grab a live entry). Everything lives on tmpfs (/dev/shm) -- tiny + space-safe.
"""
import sys, os, pathlib, shutil, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K
from lib import optrace as OT

NFILES = 3000
SIZE = 8 * 1024          # every file < parallelizeIfSmallerThan (128 KB) -> all "small"
SLOW_MS = 3              # per libc read/write; stretches the async copy past the 1.5 s stall window
INODE_THREADS = 16


def _make_tree(root):
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for i in range(NFILES):
        d = os.path.join(root, f"d{i % 8}")           # spread over 8 subdirs -> real inode work too
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"f{i:05d}.bin"), "wb") as fh:
            fh.write(bytes((i % 251,)) * SIZE)


def _one(backend):
    src = os.path.join("/dev/shm", f"sfsr_src_{backend}_{os.getpid()}")
    dest = os.path.join("/dev/shm", f"sfsr_dst_{backend}_{os.getpid()}")
    for p in (src, dest):
        shutil.rmtree(p, ignore_errors=True)
    os.makedirs(dest)
    _make_tree(src)
    trace = tempfile.mktemp(prefix="uc-optrace-sfsr-", suffix=".txt")
    ok = True
    try:
        os.environ["UC_FS_OPTRACE_PATH"] = trace
        os.environ["UC_FS_SCENARIO"] = f"slow:{SLOW_MS}"
        try:
            r = H.run(backend, "cp", [src], dest, fs_preload=K.fs_so(),
                      file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
                      expect_dir=src, inode_threads=INODE_THREADS, mem_limit_mb=1024)
        finally:
            os.environ.pop("UC_FS_OPTRACE_PATH", None)
            os.environ.pop("UC_FS_SCENARIO", None)

        base = f"alive={r.stayed_alive} completed={r.completed} content_ok={r.content_ok} mem_errors={r.mem_errors}"
        if not (r.stayed_alive and r.completed and r.content_ok and r.mem_errors == 0):
            print(f"      [{backend}] run NOT ok: {base}\n{r.diff_text[:400]}")
            ok = False

        if backend == H.ASYNC:
            t = OT.Trace(trace, dest, src)
            dup = t.problems_no_duplicate(check_data=True)
            creates = {}
            for s in t.dest_create_segments():
                creates[s.path] = creates.get(s.path, 0) + 1
            ndup = sum(1 for _p, n in creates.items() if n > 1)
            if len(t.events) == 0:
                print(f"      [{backend}] op trace EMPTY -- shim not active? (check would be vacuous)")
                ok = False
            if dup:
                print(f"      [{backend}] SPURIOUS RESET double-start: {ndup} dest file(s) create-opened >1x "
                      f"({len(dup)} duplicate-op problems). First few:")
                for p in dup[:6]:
                    print(f"        - {p}")
                ok = False
            print(f"      [{backend}] {base}  dup_create_open_paths={ndup} segments={len(t.segments)}")
        else:
            print(f"      [{backend}] {base}  (io_uring: ring I/O invisible to op trace -> regression guard only)")
    finally:
        os.unlink(trace) if os.path.exists(trace) else None
        for p in (src, dest):
            shutil.rmtree(p, ignore_errors=True)
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    bes = [b for b in (backends or [H.ASYNC, H.IO_URING]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [smallfile_scheduler_reset] SKIP (async/io_uring only; IOCP shares ListThread, "
              "covered on the laptop by iocp_parity)")
        return True
    ok = True
    for b in bes:
        ok = _one(b) and ok
    print(f"    [smallfile_scheduler_reset] {'PASS  (no spurious stall-reset / double-start on a small-file copy)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
