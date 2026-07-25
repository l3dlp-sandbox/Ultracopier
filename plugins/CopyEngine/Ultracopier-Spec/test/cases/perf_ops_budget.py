#!/usr/bin/env python3
"""PERF REGRESSION GUARD: a budget on the number of filesystem OPERATIONS per copied file.

Why ops and not seconds: a wall-clock assertion is not load-immune -- it goes red when the machine
is busy, and a flaky gate is worse than no gate (see the "FLAKY == FAILED" rule). The count of
syscalls the engine issues per file does NOT depend on machine load, so it is a stable ratchet, and
it is the quantity that actually drives the small-file throughput gap: at ~7000 files/s every extra
op per file is real time, and on a NAS (~10 ms per round-trip, measured) one extra op per file costs
10 ms * N. This is the guard that was missing when the benchmark harness silently rotted for a month.

What it asserts, per file copied (async backend, where the LD_PRELOAD shim sees the whole
decomposition -- data ops on io_uring/IOCP go through the ring/overlapped API and are invisible to
libc, so there only the metadata budget is checked):
  * total ops per file <= TOTAL_BUDGET
  * per-verb ops per file <= PER_VERB_BUDGET (no accidental double-open, double-close, extra
    chmod/utime pass, re-truncate, ...)
A REGRESSION here means the engine started doing more work per file -- exactly the kind of change
that quietly turns a 2x-vs-robocopy gap into a 3x one.

The budgets are the MEASURED current cost plus headroom, i.e. a ratchet: they may be lowered when
the engine gets leaner, never raised to make a red run pass (see the "never weaken a test" rule).
"""
import sys, os, pathlib, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

NFILES = 240          # enough for a stable average, small enough to stay a fast case
NDIRS = 12
FILESIZE = 4096       # < blockSize -> a healthy engine needs ONE read and ONE write per file
BLOB = b"p" * FILESIZE

# MEASURED BASELINE (async, 2026-07-24, 240 files x 4 KiB in 12 dirs): 8.38 ops/file total --
#   OPEN 2.00 (source+dest)   CLOSE 2.00   CHMOD 1.11   UTIME 1.11   READ 1.00   WRITE 1.00
#   MKDIR 0.16   (chmod/utime exceed 1.00 because directories get their perms+date too)
# That is already close to the theoretical minimum for a copy that preserves perms and dates, which
# is consistent with the Linux backend matching rsync in the throughput benchmark.
# Budgets = baseline + modest headroom, so a real regression trips them. Lower them when the engine
# gets leaner; NEVER raise them to dodge a failure -- a rise means more work per file.
TOTAL_BUDGET = 10.0
PER_VERB_BUDGET = {
    "OPEN": 2.5,      # source + destination
    "CLOSE": 2.5,
    "READ": 2.5,      # one full-size read (+ the 0-byte EOF read some paths do)
    "WRITE": 1.5,
    "CHMOD": 1.5,
    "UTIME": 1.5,
    "TRUNC": 1.5,
    "FALLOC": 1.5,
    "UNLINK": 0.5,    # a clean copy must not be deleting things
    "RENAME": 0.5,
    "RMDIR": 0.5,
}


def _mktree(tag):
    src = K.fresh_src_root(tag)
    files, dirs = {}, []
    per = NFILES // NDIRS
    for d in range(NDIRS):
        rel_d = "d%02d" % d
        dirs.append(rel_d)
        for i in range(per):
            rel = os.path.join(rel_d, "f%03d.dat" % i)
            K.write_file(os.path.join(src, rel), BLOB)
            files[rel] = FILESIZE
    return src, files, dirs


def _check(backend):
    src, files, dirs = _mktree(f"perfops_{backend}_src")
    dest = K.fresh_dest(f"perfops_{backend}_dest")
    data_visible = (backend == H.ASYNC)   # io_uring/IOCP data ops bypass libc -> metadata only

    r, t, exp_files, exp_dirs = K.run_traced(
        backend, "cp", src, dest, files, dirs,
        file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
        expect_dir=src)

    ok = r.ok and r.content_ok
    if not ok:
        print(f"      run not ok: completed={r.completed} alive={r.stayed_alive} "
              f"content={r.content_ok} mem_errors={r.mem_errors}\n{r.diff_text}")
    if not t.events:
        print("      op trace EMPTY -- shim not active? the budget check would be vacuous")
        return False

    counts = collections.Counter(e.verb for e in t.events)
    n = float(len(files))
    total_per_file = sum(counts.values()) / n
    print(f"      [{backend}] {int(n)} files, {sum(counts.values())} ops "
          f"-> {total_per_file:.2f} ops/file"
          f"{'' if data_visible else '  (metadata only: data ops bypass libc on this backend)'}")
    for verb, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"         {verb:8} {c:6}  {c/n:5.2f}/file")

    over = []
    # The TOTAL budget only makes sense where the whole decomposition is visible.
    if data_visible and total_per_file > TOTAL_BUDGET:
        over.append(f"total {total_per_file:.2f}/file > budget {TOTAL_BUDGET}")
    for verb, budget in PER_VERB_BUDGET.items():
        if not data_visible and verb in ("READ", "WRITE", "FALLOC"):
            continue                       # not observable on the ring/overlapped backends
        per = counts.get(verb, 0) / n
        if per > budget:
            over.append(f"{verb} {per:.2f}/file > budget {budget}")
    if over:
        print("      OVER BUDGET (the engine got heavier per file -- do NOT just raise the budget):")
        for o in over:
            print(f"        - {o}")
        ok = False
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    return K.for_backends(backends, _check)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
