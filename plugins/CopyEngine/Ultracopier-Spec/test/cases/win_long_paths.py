#!/usr/bin/env python3
r"""Windows: a destination path LONGER THAN MAX_PATH (260) must still be copied.

User report (2026-08-19): "some files fail to copy, displaying 'path not found'; this seems
to affect files with long file names -- Windows' own copy and TeraCopy handle them". That
message is FormatMessage() for ERROR_PATH_NOT_FOUND (3), which is exactly what CreateFileW
returns for a >259-char path when the path is NOT given in the \\?\ extended form (this box
has HKLM\...\FileSystem\LongPathsEnabled=0, the Windows default).

The engine's own answer to this is TransferThread::toFinalPath(), which prefixes \\?\
(\\?\UNC\ for a NAS path). Every Win32 site in the engine uses it -- scan (FindFirstFileW),
mkdir, stat/attributes, unlink/rmdir, CopyFileExW, the IOCP data plane, the date/permission
writers. Measured on the Windows 10 laptop with a 288-char path (test scratch):
    CreateFileW("C:\...288 chars...")     -> FAIL, GetLastError()=3 (path not found)
    CreateFileW("\\?\C:\...288 chars...") -> OK
So a single unprefixed open is enough to make a file uncopyable while its short siblings copy
fine -- which is precisely the reported symptom.

This case pins the behaviour end-to-end on the real box: three source files whose destination
paths straddle MAX_PATH -- a short control, a 250-byte FILE NAME, and a deep directory chain --
must all arrive with byte-correct content.

The source tree is created ON the box through the .NET \\?\ APIs rather than pushed with tar:
bsdtar (and the shell) are themselves MAX_PATH-limited, so a pushed tree fails to EXTRACT and
would make the case fail for a staging reason instead of an engine reason (that is what
`iocp_run.py long_paths` hits -- its diff reports the deep files "missing on box" even when the
engine is fine). Setup and verification therefore both go through \\?\.

Windows lane only (MAX_PATH is a Win32 concept); skips cleanly when no [windows] host is
configured. The Linux side of path-length robustness is cases/long_paths.py."""
import sys, pathlib
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K, winlane

SRC_ROOT = r"C:\cc-test\uc-lp\src"        # 20 chars
LONG_DIR = "A" * 200
DEEP_DIR = "B" * 60
LONG_NAME = ("L" * 246) + ".dat"          # 250-byte single component (<= NTFS 255)

# {relative path on the source tree: content}. The dest adds ~35 chars
# (C:\cc-test\uc-auto\run-XXXXXXXX\dst\src\), so every long entry lands well past 260.
FILES = {
    "normal.txt": "short-sibling-control",
    LONG_NAME: "long-single-component-name",
    LONG_DIR + "\\" + DEEP_DIR + "\\deep.txt": "deep-nested-long-path",
}


def _ps_setup() -> str:
    lines = ["$ErrorActionPreference='Stop'",
             f"$s='\\\\?\\{SRC_ROOT}'",
             "if ([System.IO.Directory]::Exists($s)) { [System.IO.Directory]::Delete($s, $true) }",
             "[System.IO.Directory]::CreateDirectory($s) | Out-Null"]
    for rel, content in FILES.items():
        parent = rel.rsplit("\\", 1)[0] if "\\" in rel else ""
        if parent:
            lines.append(f"[System.IO.Directory]::CreateDirectory($s + '\\{parent}') | Out-Null")
        lines.append(f"[System.IO.File]::WriteAllText($s + '\\{rel}', '{content}')")
        lines.append(f"Write-Output ('SETUP=' + [System.IO.File]::Exists($s + '\\{rel}'))")
    return "\n".join(lines)


def _ps_verify(copied: str) -> str:
    lines = ["$ErrorActionPreference='SilentlyContinue'", f"$d='\\\\?\\{copied}'"]
    for i, rel in enumerate(FILES):
        lines.append(f"$p{i}=$d + '\\{rel}'")
        lines.append(f"if ([System.IO.File]::Exists($p{i}))"
                     f" {{ Write-Output ('F{i}=' + [System.IO.File]::ReadAllText($p{i})) }}"
                     f" else {{ Write-Output 'F{i}=<MISSING>' }}")
    return "\n".join(lines)


def run(backends=None, memcheck=H.NONE) -> bool:
    # Windows lane only; a Linux-restricted invocation is a no-op pass.
    if backends is not None and H.IOCP not in backends:
        return True
    cfg = H.load_config()
    cfg.set("paths", "SOURCEWINDOWS", "")     # never touch the operator's real source tree
    if not cfg.get("windows", "host", fallback="").strip():
        print("    [iocp] SKIP (no [windows] host in config.ini -> Windows lane disabled)")
        return True

    box = winlane._Box(cfg.get("windows", "host").strip())
    setup = box.ps(_ps_setup(), timeout=180)
    if setup.stdout.count("SETUP=True") != len(FILES):
        print(f"    [iocp] setup of the >MAX_PATH source tree failed: "
              f"{setup.stdout.strip()[:400]} {setup.stderr.strip()[:200]}")
        return False

    def post_verify(b, dest, srcs):
        copied = winlane.win_join(dest, "src")
        out = b.ps(_ps_verify(copied), timeout=180).stdout
        problems = []
        for i, (rel, content) in enumerate(FILES.items()):
            got = None
            for line in out.splitlines():
                if line.strip().startswith(f"F{i}="):
                    got = line.strip()[len(f"F{i}="):]
            shown = rel if len(rel) <= 48 else f"{rel[:20]}...{rel[-20:]} ({len(rel)}b)"
            if got is None:
                problems.append(f"{shown}: no answer from the box")
            elif got == "<MISSING>":
                problems.append(f"{shown}: NOT COPIED (dest path {len(copied) + 1 + len(rel)} chars)")
            elif got != content:
                problems.append(f"{shown}: content {got!r} != {content!r}")
        if problems:
            return False, "long-path failures: " + "; ".join(problems)
        return True, f"all {len(FILES)} files copied past MAX_PATH (longest dest {max(len(copied) + 1 + len(r) for r in FILES)} chars)"

    try:
        r = winlane.run_windows("cp", [], cfg=cfg, source_on_box=SRC_ROOT,
                                file_collision=H.FileCollision.OVERWRITE,
                                folder_collision=H.FolderCollision.MERGE,
                                file_error=H.FileError.SKIP,
                                expect=None, post_verify=post_verify, stay_alive_seconds=5)
    finally:
        # The source tree lives outside the run sandbox winlane cleans, and is itself past
        # MAX_PATH -- remove it through \\?\ (Remove-Item would fail on the deep entries).
        box.ps(f"$s='\\\\?\\{SRC_ROOT.rsplit(chr(92), 1)[0]}'; "
               "if ([System.IO.Directory]::Exists($s)) "
               "{ [System.IO.Directory]::Delete($s, $true) }", timeout=180)

    if not r.ok:
        print(f"      [iocp] FAIL: completed={r.completed} alive={r.stayed_alive} "
              f"content={r.content_ok} crashes={r.mem_errors} notes={r.notes}")
    else:
        print(f"    [iocp] {r.notes}")
    return r.ok


if __name__ == "__main__":
    sys.exit(0 if run(backends=[H.IOCP]) else 1)
