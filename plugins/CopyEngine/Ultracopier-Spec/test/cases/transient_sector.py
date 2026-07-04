#!/usr/bin/env python3
"""Transient (recoverable) sector: a source file whose reads fail EIO the first few
times, then succeed -- exactly what a flaky dying-HDD sector does (it reads on retry).
The healthy backup behaviour is to NOT give up after one error.

We name the flaky file to contain 'RECOVER' and inject:
    UC_FS_SCENARIO = "flaky:RECOVER:2"
i.e. the first 2 read attempts on that path (across the whole process) fail EIO, then
every read succeeds.

Put-to-end semantics (see the CLAUDE.md "Put to end" note): a failing file is deferred ONE pass
and retried; if it STILL fails after that one deferred retry, the engine escalates to the Ask dialog
(never a 16x retry storm). flaky:RECOVER:2 fails attempt 1 (-> put-to-end) and attempt 2 (the deferred
retry) -> the dialog is shown -> the scripted answer here is RETRY -> attempt 3 reads clean (the flaky
budget is spent) and the file is copied in full. This is exactly the design's recovery path: one auto
retry, then ASK, and a genuinely flaky sector recovers when the user (here the hook) chooses Retry.

ASSERTS:
  * r.completed and r.stayed_alive   -- no hang, no crash on the transient error
  * the RECOVER file is EVENTUALLY backed up BYTE-PERFECT (recovery succeeded)
  * every other (always-good) file is byte-perfect too

ASYNC ONLY: the flaky verb faults libc source reads, which only the async backend's
ReadThread uses (io_uring data-plane reads go through io_uring_enter(2), not libc)."""
import sys, pathlib, os, filecmp
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import synthtree, casekit as K

RECOVER_NAME = "report_RECOVER_sector.bin"


def _build_tree(root: str):
    """A 'default' tree plus one >1 MiB file named to contain 'RECOVER'."""
    root = os.path.abspath(root)
    synthtree.make_tree(root, "default")
    recover = pathlib.Path(root) / RECOVER_NAME
    recover.write_bytes(synthtree._seeded_bytes_fast(7777, 1024 * 1024 + 321))
    return root


def run(backends=None, memcheck=H.NONE) -> bool:
    src = _build_tree(K.fresh_src_root("transient_src"))
    base = os.path.basename(src)

    def one():
        dest = K.fresh_dest("transient_dest")
        copied = os.path.join(dest, base)
        K.with_scenario("flaky:RECOVER:2")
        # put-to-end gives ONE deferred retry then escalates to Ask; the scripted dialog answers RETRY,
        # so attempt 3 (flaky budget spent) reads clean and the flaky sector recovers.
        os.environ["ULTRACOPIER_TEST_FILE_ERROR_ACTION"] = "retry"
        try:
            r = H.run(H.ASYNC, "cp", [src], dest,
                      file_collision=H.FileCollision.OVERWRITE,
                      folder_collision=H.FolderCollision.MERGE,
                      file_error=H.FileError.PUT_TO_END,   # defer once, then ask -> retry -> recovers
                      expect_dir=None,
                      fs_preload=K.fs_so())
        finally:
            K.with_scenario("")
            os.environ.pop("ULTRACOPIER_TEST_FILE_ERROR_ACTION", None)

        resilient = r.completed and r.stayed_alive
        # The RECOVER file must EVENTUALLY be present and byte-identical (recovered).
        recovered_src = os.path.join(src, RECOVER_NAME)
        recovered_dst = os.path.join(copied, RECOVER_NAME)
        recovered = (os.path.exists(recovered_dst)
                     and filecmp.cmp(recovered_src, recovered_dst, shallow=False))
        # And the always-good files must be byte-perfect.
        good_ok = True
        for dirpath, _dirs, files in os.walk(src):
            for fn in files:
                if fn != RECOVER_NAME:
                    sp = os.path.join(dirpath, fn)
                    dp = os.path.join(copied, os.path.relpath(sp, src))
                    if not (os.path.exists(dp) and filecmp.cmp(sp, dp, shallow=False)):
                        good_ok = False

        ok = (resilient and recovered and good_ok
              and not r.oom_killed and r.mem_errors == 0)
        if not ok:
            print(f"      not ok: completed={r.completed} alive={r.stayed_alive} "
                  f"recovered={recovered} good_ok={good_ok} "
                  f"oom={r.oom_killed} mem_errors={r.mem_errors}")
        return ok

    res = one()
    print(f"    [async flaky:RECOVER:2] {'PASS' if res else 'FAIL'}")
    return res


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
