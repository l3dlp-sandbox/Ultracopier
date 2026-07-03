#!/usr/bin/env python3
"""#HIGH (2026-07-02 coverage review): a dying-disk DIRECTORY whose own blocks hit a bad sector makes
readdir() return NULL mid-listing (errno=EIO). TransferThread::entryInfoList treated that NULL as a
clean end-of-directory (`return true` with a PARTIAL list), so the scan considered the folder FULLY
read and SILENTLY dropped every entry after the fault -- NO error, NO warning. On a dying-disk backup
the user thinks they got the whole folder; they didn't. That violates the core salvage promise ("copy
what's readable AND inform the user").

FIX: entryInfoList sets errno=0 before each readdir and, on a NULL return with errno set, returns FALSE
(the caller ScanFileOrFolder emits errorOnFolder -> the folderError policy is applied and, with Ask, a
dialog is raised). The entries read BEFORE the fault stay in the list and are still copied (best-effort
salvage). It also list.clear()s at entry so a folder-error RETRY re-reads fresh (no duplicate entries).

Injected with the shim verb `readdirfail:<abs-source-faultdir>:<K>` -> readdir on THAT directory returns
NULL+EIO after K real entries. The substr is the SOURCE faultdir's absolute /dev/shm path; the dest sits
on a DISTINCT /dev/shm prefix so the copied tree (which mirrors the source suffix) is NOT faulted.

ASSERTS (async + io_uring; the SOURCE scan uses libc readdir for BOTH backends):
  * the error IS SURFACED -- folderError=Ask routes the folder-read error through
    FileErrorDialog::createInstance (the test hook), which appends to ULTRACOPIER_TEST_ERROR_MARKER. The
    marker MUST exist + be non-empty. RED before the fix (readdir NULL swallowed as EOF -> no error ->
    marker absent); GREEN after.
  * best-effort salvage: the readable PREFIX of faultdir is copied (1..K files present), the process
    stays alive/completes, no crash, no mem errors.
  * the error does NOT leak: a healthy SIBLING directory is copied IN FULL, and the top-level file too.
"""
import sys, os, pathlib, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

NFAULT = 12          # files in the faulting directory
NGOOD = 5            # files in the healthy sibling
FAULT_AFTER = 5      # readdir faults after this many REAL entries of faultdir


def _build(root):
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "faultdir"))
    os.makedirs(os.path.join(root, "gooddir"))
    for i in range(NFAULT):
        with open(os.path.join(root, "faultdir", f"f{i:02d}.dat"), "wb") as f:
            f.write(f"faultdir file {i}\n".encode())
    for i in range(NGOOD):
        with open(os.path.join(root, "gooddir", f"g{i:02d}.dat"), "wb") as f:
            f.write(f"gooddir file {i}\n".encode())
    with open(os.path.join(root, "top.txt"), "wb") as f:
        f.write(b"top level\n")


def _one(backend):
    src = os.path.join("/dev/shm", f"fdl_src_{backend}_{os.getpid()}")
    dest = os.path.join("/dev/shm", f"fdl_dst_{backend}_{os.getpid()}")   # DISTINCT prefix (not fdl_src)
    marker = os.path.join("/dev/shm", f"fdl_marker_{backend}_{os.getpid()}.txt")
    for p in (src, dest):
        shutil.rmtree(p, ignore_errors=True)
    for p in (marker,):
        try: os.unlink(p)
        except OSError: pass
    os.makedirs(dest)
    _build(src)
    fault_target = os.path.join(src, "faultdir")   # absolute -> matches ONLY the source faultdir
    ok = True
    try:
        K.with_scenario(f"readdirfail:{fault_target}:{FAULT_AFTER}")
        os.environ["ULTRACOPIER_TEST_FILE_ERROR_ACTION"] = "skip"   # hook returns Skip for the folder error
        os.environ["ULTRACOPIER_TEST_ERROR_MARKER"] = marker
        try:
            r = H.run(backend, "cp", [src], dest, fs_preload=K.fs_so(),
                      file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
                      folder_error=H.FolderError.ASK,          # Ask -> dialog -> hook fires + marker written
                      file_error=H.FileError.SKIP,
                      expect_dir=None, inode_threads=4, mem_limit_mb=1024)
        finally:
            K.with_scenario("")
            os.environ.pop("ULTRACOPIER_TEST_FILE_ERROR_ACTION", None)
            os.environ.pop("ULTRACOPIER_TEST_ERROR_MARKER", None)
        copied = os.path.join(dest, os.path.basename(src))

        if not (r.stayed_alive and r.completed and r.mem_errors == 0):
            print(f"      [{backend}] run NOT ok: alive={r.stayed_alive} completed={r.completed} "
                  f"mem_errors={r.mem_errors}")
            ok = False
        # (1) the error was SURFACED (marker written by the folder-error dialog hook)
        surfaced = os.path.exists(marker) and os.path.getsize(marker) > 0
        if not surfaced:
            print(f"      [{backend}] folder-read error was NOT surfaced (marker absent) -- silent drop!")
            ok = False
        # (2) best-effort salvage: a readable PREFIX of faultdir copied (1..NFAULT-1), not zero, not all
        fd = os.path.join(copied, "faultdir")
        nfault_copied = len([n for n in os.listdir(fd)]) if os.path.isdir(fd) else 0
        if not (1 <= nfault_copied <= NFAULT - 1):
            print(f"      [{backend}] faultdir salvage wrong: {nfault_copied} files copied "
                  f"(expected a partial 1..{NFAULT-1})")
            ok = False
        # (3) the error did NOT leak: the healthy sibling + top file copied in full
        gd = os.path.join(copied, "gooddir")
        ngood = len([n for n in os.listdir(gd)]) if os.path.isdir(gd) else 0
        top_ok = os.path.exists(os.path.join(copied, "top.txt"))
        if ngood != NGOOD or not top_ok:
            print(f"      [{backend}] error LEAKED: gooddir={ngood}/{NGOOD} top_ok={top_ok} "
                  f"(a readdir fault in one dir must not drop healthy siblings)")
            ok = False
        print(f"      [{backend}] surfaced={surfaced} faultdir_salvaged={nfault_copied}/{NFAULT} "
              f"gooddir={ngood}/{NGOOD} top={top_ok} alive={r.stayed_alive}")
    finally:
        for p in (src, dest):
            shutil.rmtree(p, ignore_errors=True)
        try: os.unlink(marker)
        except OSError: pass
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    bes = [b for b in (backends or [H.ASYNC, H.IO_URING]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [faulty_dir_listing] SKIP (async/io_uring only; Windows uses FindFirstFile)")
        return True
    ok = True
    for b in bes:
        ok = _one(b) and ok
    print(f"    [faulty_dir_listing] {'PASS  (partial readdir surfaced as an error; readable prefix salvaged; siblings intact)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
