#!/usr/bin/env python3
"""keepDate must copy a file whose modification date PREDATES 1995, not refuse the file.

User report (2026-08-19): with "Keep file date" enabled, dragging a single file OUT of a
ZIP/7z archive opened in 7-Zip raises an Ultracopier error about the modification date and
the file is never extracted. A ZIP stores MS-DOS timestamps whose epoch is 1980-01-01, so a
file extracted from an archive very often carries a 1980..1994 mtime -- a perfectly valid,
readable, settable date on both POSIX (utime) and Windows (SetFileTime).

The engine treats such a date as "unable to read the date":
  TransferThread::readSourceFileDateTime() returns FALSE when the source mtime is below
  ULTRACOPIER_PLUGIN_MINIMALYEAR_TIMESTAMPS (788965200 == 1995-01-01), and the transfer
  backends escalate that failure when keepDate is on --
    emit errorOnFile(source, "Wrong modification date or unable to get it, ...") + return
  -- so the FILE IS NOT COPIED AT ALL (with fileError=Skip it is silently dropped; with the
  shipping fileError=Ask the user gets the dialog the report describes).

Losing a readable file because its date is old is the "backup misses good data" failure this
engine must never have, so the case asserts the CORRECT behaviour:

  * every file is copied (content diff clean), whatever its mtime, and
  * its mtime is reproduced on the destination (keepDate=true means keep the date, including
    a 1980 one).

The tree straddles the 1995 boundary so a regression is legible: 1980-01-01 (the DOS/ZIP zero
date), one second BELOW the constant, one second ABOVE it, and a modern date. Metadata/date
handling is shared code, so it runs on async + io_uring, and on IOCP via the Windows lane
(the same MINIMALYEAR gate sits in the Q_OS_WIN32 branch of readSourceFileDateTime)."""
import sys, pathlib, os
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

MINIMALYEAR = 788965200          # ULTRACOPIER_PLUGIN_MINIMALYEAR_TIMESTAMPS (1995-01-01 UTC)
FILES = {
    "from_zip_dos_epoch.txt": 315532800,      # 1980-01-01: the DOS/ZIP zero date (the report)
    "just_below_1995.txt":    MINIMALYEAR - 1,
    "just_above_1995.txt":    MINIMALYEAR + 1,
    "modern.txt":             1600000000,     # 2020-09-13
}


def _make_tree(root: str) -> str:
    import shutil
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    for name, mt in FILES.items():
        for rel in (name, os.path.join("sub", name)):
            p = os.path.join(root, rel)
            with open(p, "wb") as fh:
                fh.write(b"archive-extract-" + name.encode() + b"-" + b"Z" * 512)
            os.utime(p, (mt, mt))
    return root


def _date_problems(src_root: str, dst_root: str) -> list:
    probs = []
    for rel_dir in ("", "sub"):
        for name, mt in FILES.items():
            rel = os.path.join(rel_dir, name) if rel_dir else name
            dp = os.path.join(dst_root, rel)
            if not os.path.exists(dp):
                probs.append(f"{rel}: MISSING in destination (mtime {mt} refused the copy)")
            else:
                got = int(os.lstat(dp).st_mtime)
                if abs(got - mt) > 1:
                    probs.append(f"{rel}: mtime {got} != source {mt}")
    return probs


def run(backends=None, memcheck=H.NONE) -> bool:
    src = _make_tree(K.fresh_src_root("olddate_src"))

    def one(backend):
        K.with_scenario("")
        dest = K.fresh_dest("olddate_dest")
        # fileError=Skip so a refused file is DROPPED (observable as a missing file) instead of
        # blocking on the Ask dialog the user sees; keepDate=true is what arms the escalation.
        r = H.run(backend, "cp", [src], dest, keep_date=True, do_right=True,
                  file_collision=H.FileCollision.OVERWRITE,
                  folder_collision=H.FolderCollision.MERGE,
                  file_error=H.FileError.SKIP,
                  expect_dir=src, memcheck=memcheck)
        probs = _date_problems(src, K.copied_root(dest, src))
        if not (r.ok and r.content_ok):
            print(f"      not ok: completed={r.completed} alive={r.stayed_alive} "
                  f"content={r.content_ok} mem_errors={r.mem_errors}\n{r.diff_text}")
        if probs:
            print("      old-date files not copied / date not kept:")
            for p in probs:
                print(f"        - {p}")
        return r.ok and r.content_ok and not probs

    def iocp_one():
        from lib import winlane
        if not K.windows_host_configured():
            print("    [iocp] SKIP (no Windows host configured)")
            return True

        def pv(box, dest, srcs):
            copied = winlane.win_join(dest, os.path.basename(src))
            problems = []
            for rel_dir in ("", "sub"):
                for name, mt in FILES.items():
                    rel = f"{rel_dir}\\{name}" if rel_dir else name
                    m = winlane.win_meta(box, winlane.win_join(copied, rel))
                    if not m:
                        problems.append(f"{rel}: missing on box (date refused the copy)")
                    elif abs(m.get("mtime", 0) - mt) > 2:
                        problems.append(f"{rel}: LastWriteTime {m.get('mtime')} != {mt}")
            if problems:
                return False, "old dates: " + "; ".join(problems[:6])
            return True, "every file copied, mtime kept (incl. the 1980 DOS date)"

        r = winlane.run_windows("cp", [src], keep_date=True, do_right=True,
                                file_collision=H.FileCollision.OVERWRITE,
                                folder_collision=H.FolderCollision.MERGE,
                                file_error=H.FileError.SKIP,
                                expect=src, post_verify=pv)
        if not r.ok:
            print(f"      [iocp] FAIL: completed={r.completed} alive={r.stayed_alive} "
                  f"content={r.content_ok} crashes={r.mem_errors} notes={r.notes}")
        return r.ok

    return K.for_option_backends(backends, one, iocp_one)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
