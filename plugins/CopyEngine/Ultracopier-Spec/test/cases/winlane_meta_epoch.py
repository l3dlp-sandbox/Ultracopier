#!/usr/bin/env python3
"""winlane.win_meta() must report a Windows LastWriteTime as a TRUE unix epoch.

WHY THIS EXISTS: win_meta built its epoch baseline with `Get-Date '1970-01-01 00:00:00Z'`,
which PowerShell CONVERTS to local time (Kind=Local). .NET subtracts two DateTime values on
raw ticks and ignores Kind, so the baseline was off by the box's UTC offset and every mtime
read back came out shifted by that amount -- +14400s on this UTC-4 box. On a UTC box the
offset is 0, so the bug was invisible there and only surfaced when the lab moved to UTC-4:
opt_perms_dates then reported "date not preserved" for files whose date the engine had in
fact preserved perfectly. A metadata reader that lies makes every option assertion built on
it meaningless, in EITHER direction (it can also mask a real date bug by 4h).

The assertion is EXACT (no tolerance): a Kind/timezone mix-up can only reappear as a whole
number of hours, so any tolerance would hide exactly the class of bug this guards. The
expected instant is pinned in UTC on both sides -- the file's date is stamped via
LastWriteTimeUtc and compared against the constant below -- so the case is correct on a box
in ANY timezone, which is the property that was missing.

Windows lane only (needs the box); skipped cleanly elsewhere, like every other windows_* case.
"""
import sys, pathlib
_TEST_DIR = pathlib.Path(__file__).resolve().parents[1]
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(_TEST_DIR))
from lib import harness as H

# 2001-02-03 04:05:06 UTC -- the same instant make_meta_tree stamps, so a regression here and
# a regression in opt_perms_dates point at the same number.
EXPECTED_EPOCH = 981173106
REMOTE_NAME = "uc_winlane_meta_epoch.txt"


def run(backends=None, memcheck=H.NONE) -> bool:
    cfg = H.load_config()
    if not cfg.has_section("windows") or not cfg.get("windows", "host"):
        print("    [windows] SKIP (no Windows host configured)")
        return True
    if backends and H.IOCP not in backends:
        print(f"    [windows] SKIP (requested backends {backends} do not include IOCP)")
        return True

    from lib import winlane
    box = winlane._Box(cfg.get("windows", "host"))

    # Stamp a file with a known UTC instant, read it back through win_meta, and drop it.
    stamp = (f"$p=Join-Path $env:TEMP '{REMOTE_NAME}'; "
             f"Set-Content -LiteralPath $p -Value 'x' -Force; "
             f"$d=New-Object DateTime(2001,2,3,4,5,6,[DateTimeKind]::Utc); "
             f"$f=Get-Item -LiteralPath $p -Force; $f.LastWriteTimeUtc=$d; "
             f"Write-Output ('PATH='+$p); "
             f"Write-Output ('OFFSET='+[System.TimeZoneInfo]::Local.BaseUtcOffset.TotalSeconds)")
    out = box.ps(stamp)
    path, offset = None, None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("PATH="):
            path = line[5:]
        elif line.startswith("OFFSET="):
            offset = line[7:]
    if not path:
        print(f"    [windows] FAIL: could not stamp the probe file: {out.stdout.strip()[:200]}")
        return False

    meta = winlane.win_meta(box, path)
    box.ps(f"Remove-Item -LiteralPath '{path}' -Force -ErrorAction SilentlyContinue")

    got = meta.get("mtime")
    if got != EXPECTED_EPOCH:
        delta = "n/a" if got is None else got - EXPECTED_EPOCH
        print(f"    [windows] FAIL: win_meta mtime={got} expected={EXPECTED_EPOCH} "
              f"delta={delta}s (box UTC offset={offset}s -- a delta equal to it means the "
              f"epoch baseline went through local time again)")
        return False

    print(f"    [windows] PASS  (win_meta returns a true UTC epoch; box offset={offset}s)")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
