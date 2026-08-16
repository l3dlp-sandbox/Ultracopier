#!/usr/bin/env python3
"""Windows Alternate Data Stream (ADS) handling.

NTFS allows multiple data streams per file (e.g., `file.txt:stream`). Ultracopier
may ignore them (copy only the default stream) or preserve them (if the engine
supports ADS). This test creates a file with an ADS, copies it, and verifies at
least the default stream is correct; the ADS may be missing (ignored) or present
(preserved) — either is acceptable as long as the copy does not crash.

Only runs on the Windows lane (IOCP backend). On Linux, the case is skipped.
"""
import sys, pathlib, os, subprocess, tempfile, shutil, uuid
_TEST_DIR = pathlib.Path(__file__).resolve().parents[1]
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(_TEST_DIR))
from lib import harness as H
from lib import casekit as K


def run(backends=None, memcheck=H.NONE) -> bool:
    # Only run on Windows lane (IOCP). If no Windows host configured, skip.
    cfg = H.load_config()
    if not cfg.has_section("windows") or not cfg.get("windows", "host"):
        print("    [windows] SKIP (no Windows host configured)")
        return True

    # Only IOCP backend; async/io_uring are Linux-only.
    if backends and H.IOCP not in backends:
        print(f"    [windows] SKIP (requested backends {backends} do not include IOCP)")
        return True

    # Create a temporary directory on the Windows host.
    from lib import winlane
    box = winlane._Box(cfg.get("windows", "host"))
    # We'll stage a source tree with a file containing an ADS.
    # Use PowerShell to create ADS: `echo ads_content > file.txt:stream`
    # We'll create a small source tree, copy it via ultracopier, then verify.
    # Since we cannot rely on the Windows host having a temp location, we'll use
    # the winlane staging mechanism: push a tarball.
    tmp_lines = box.ps("$env:TEMP").stdout.splitlines()
    if not tmp_lines:
        print("    [windows] SKIP (could not get TEMP)")
        return True
    tmp = tmp_lines[0].strip()
    src_name = f"ads_test_{uuid.uuid4().hex[:8]}"
    src = f"{tmp}\\{src_name}"
    # No dest of our own: run_windows owns the sandbox it copies into (base\src, base\dst),
    # so the assertions run through post_verify against the destination it actually used.
    box.ps(f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{src}'")
    # Create source directory with a normal file and a file with ADS.
    box.ps(f"New-Item -ItemType Directory -Path '{src}' | Out-Null")
    box.ps(f"Set-Content -Path '{src}\\normal.txt' -Value 'hello world'")
    # Create ADS: write to file:stream
    box.ps(f"Set-Content -Path '{src}\\with_ads.txt' -Value 'default stream'")
    box.ps(f"Set-Content -Path '{src}\\with_ads.txt:secret' -Value 'alternate data'")
    # Also create a subdirectory with a file that has ADS.
    box.ps(f"New-Item -ItemType Directory -Path '{src}\\sub' | Out-Null")
    box.ps(f"Set-Content -Path '{src}\\sub\\deep.txt' -Value 'deep default'")
    box.ps(f"Set-Content -Path '{src}\\sub\\deep.txt:extra' -Value 'deep extra'")

    # The source tree lives ON the box (an ADS cannot exist on a Linux filesystem), so it is
    # declared with source_on_box. Passing a Windows path in sources_local -- as this case used
    # to -- made run_windows tar it on the LINUX side: "tar: : Cannot open: No such file or
    # directory", an infrastructure failure that looked like an engine failure.
    from lib import winlane

    def pv(box_, dest, srcs):
        copied = winlane.win_join(dest, src_name)
        problems = []

        def default_stream(rel, expected):
            path = winlane.win_join(copied, rel)
            out = box_.ps(f"Get-Content -Raw -LiteralPath '{path}' "
                          f"-ErrorAction SilentlyContinue").stdout.splitlines()
            if not out or out[0].strip() != expected:
                problems.append(f"{rel}: default stream {out[:1]} != {expected!r}")

        default_stream("normal.txt", "hello world")
        default_stream("with_ads.txt", "default stream")
        default_stream("sub\\deep.txt", "deep default")

        # The ADS itself MAY be dropped -- copying only the default stream is acceptable. But a
        # stream that IS carried over must be byte-correct: a truncated or garbled ADS would be
        # silent corruption, which is exactly what must never pass unnoticed.
        def ads(rel, stream, expected):
            path = winlane.win_join(copied, rel)
            out = box_.ps(f"(Get-Item -LiteralPath '{path}' -Stream * -ErrorAction "
                          f"SilentlyContinue | Select-Object -ExpandProperty Stream)"
                          ).stdout.splitlines()
            if stream in [x.strip() for x in out if x.strip()]:
                got = box_.ps(f"Get-Content -Raw -LiteralPath '{path}' -Stream {stream} "
                              f"-ErrorAction SilentlyContinue").stdout.splitlines()
                if not got or got[0].strip() != expected:
                    problems.append(f"{rel}:{stream} preserved but WRONG: "
                                    f"{got[:1]} != {expected!r}")
                else:
                    print(f"      ADS '{stream}' preserved with correct content")
            else:
                print(f"      ADS '{stream}' not present (accepted: default stream only)")

        ads("with_ads.txt", "secret", "alternate data")
        ads("sub\\deep.txt", "extra", "deep extra")
        if problems:
            return False, "ADS/default-stream problems: " + "; ".join(problems[:5])
        return True, "default streams correct; ADS absent or intact"

    result = winlane.run_windows(
        "cp", [src], source_on_box=src, cfg=cfg,
        file_collision=H.FileCollision.OVERWRITE,
        folder_collision=H.FolderCollision.MERGE,
        file_error=H.FileError.SKIP, folder_error=H.FolderError.SKIP,
        keep_date=True, do_right=True, expect=None,
        mem_limit_mb=1024, stay_alive_seconds=10, post_verify=pv,
    )
    box.ps(f"Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{src}'")
    print(f"      [iocp] completed={result.completed} alive={result.stayed_alive} "
          f"content={result.content_ok} mem_err={result.mem_errors} {result.notes}")
    print(f"    [iocp] {'PASS' if result.ok else 'FAIL'}")
    return result.ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)