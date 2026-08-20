#!/usr/bin/env python3
r"""Build + run the standalone TransferThread::toFinalPath() unit test on the Windows box.

toFinalPath() is the Win32 extended-path prefixer (\\?\ , \\?\UNC\ for a NAS) that every
filesystem call in the engine goes through; it is what lifts the MAX_PATH (260) limit, so a
defect there either loses long paths (ERROR_PATH_NOT_FOUND -- see cases/win_long_paths.py) or
silently mangles a name. Two properties are asserted by the C++ test (test/unit/finalpath_test.cpp),
on BOTH overloads -- std::string (8-bit) and std::wstring (16-bit):

  * the prefixing rules themselves (local / UNC / bare drive / '/'->'\' normalisation), and a
    >MAX_PATH path coming out prefixed and intact;
  * LENGTH-DELIMITED fidelity: paths are carried as std::string/std::wstring precisely so a
    buffer holding an embedded 0x00 ("XXXXX\0XXXX") keeps its exact size and is transformed
    byte-for-byte -- never cut at the NUL by a strlen()/c_str() step. The sharpest check is a
    '/' AFTER the NUL: a strlen-based implementation stops scanning there and leaves the
    separator unconverted. (Verified to have real discriminating power: a deliberately
    strlen-based build of toFinalPath fails 9 of the 21 checks.)

The function is #ifdef Q_OS_WIN32, so this cannot run on Linux: the test is cross-built with
the MXE mingw/Qt6 toolchain and executed ON THE REAL WINDOWS BOX (never under wine), next to
the Qt DLLs the configured [windows] exe already ships with. It self-skips (PASS) when the MXE
toolchain or the [windows] host is unavailable, exactly like the other Windows-lane cases.

The POSIX side of the same invariant is cases/pathtree_unit.py (PathTree resolve/name) and the
weird_names embedded-NUL note."""
import sys, os, pathlib, subprocess, tempfile, shutil
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import winlane

_UNIT_DIR = pathlib.Path(__file__).resolve().parents[1] / "unit"
_PRO = _UNIT_DIR / "finalpath_test.pro"
_MXE_QMAKE_DEFAULT = "/mnt/data/perso/progs/mxe/x86_64/usr/x86_64-w64-mingw32.shared/qt6/bin/qmake6"
_MXE_BIN_DEFAULT = "/mnt/data/perso/progs/mxe/x86_64/usr/bin"


def _mxe_qmake(cfg) -> str:
    """The MXE Qt6 qmake used for every Windows cross-build here (config override:
    [windows] mxe_qmake, env override: UC_MXE_QMAKE)."""
    for cand in (os.environ.get("UC_MXE_QMAKE", ""),
                 cfg.get("windows", "mxe_qmake", fallback=""),
                 _MXE_QMAKE_DEFAULT):
        if cand and os.path.exists(cand):
            return cand
    return ""


def _build(qmake: str) -> str:
    """Cross-build finalpath_test.exe; returns its local path ('' on failure)."""
    bdir = pathlib.Path(tempfile.mkdtemp(prefix="uc-finalpath-"))
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(qmake) + os.pathsep + _MXE_BIN_DEFAULT + os.pathsep + env.get("PATH", "")
    q = subprocess.run([qmake, "-o", str(bdir / "Makefile"), str(_PRO),
                        "CONFIG+=release", "CONFIG+=nodebug"],
                       cwd=bdir, env=env, capture_output=True, text=True, timeout=300)
    if q.returncode != 0:
        print(f"    [iocp] qmake failed:\n{(q.stdout + q.stderr)[-1500:]}")
        return ""
    m = subprocess.run(["make", f"-j{os.cpu_count() or 4}"], cwd=bdir, env=env,
                       capture_output=True, text=True, timeout=1800)
    exe = bdir / "release" / "finalpath_test.exe"
    if m.returncode != 0 or not exe.exists():
        print(f"    [iocp] build failed:\n{(m.stdout + m.stderr)[-2500:]}")
        return ""
    return str(exe)


def run(backends=None, memcheck=H.NONE) -> bool:
    # Windows-only function -> Windows lane only; a Linux-restricted invocation is a no-op pass.
    if backends is not None and H.IOCP not in backends:
        return True
    cfg = H.load_config()
    host = cfg.get("windows", "host", fallback="").strip()
    exe = cfg.get("windows", "exe", fallback="").strip()
    if not host or not exe:
        print("    [iocp] SKIP (no [windows] host/exe in config.ini -> Windows lane disabled)")
        return True
    qmake = _mxe_qmake(cfg)
    if not qmake:
        print("    [iocp] SKIP (MXE Qt6 qmake not found; set [windows] mxe_qmake or UC_MXE_QMAKE)")
        return True

    local_exe = _build(qmake)
    if not local_exe:
        return False

    # Run it next to the Qt DLLs the deployed ultracopier.exe already ships with.
    exe_dir = exe.rsplit("\\", 1)[0] if "\\" in exe else exe
    remote = winlane.win_join(exe_dir, "finalpath_test.exe")
    box = winlane._Box(host)
    try:
        subprocess.run(["scp", "-q", "-o", "ConnectTimeout=15", local_exe,
                        f"{host}:finalpath_test.exe"], check=True, timeout=300)
        # scp lands it in the box's HOME (the only form OpenSSH-on-Windows scp handles
        # reliably); move it next to the Qt DLLs before running.
        box.ps("Move-Item -Force -LiteralPath (Join-Path $env:USERPROFILE 'finalpath_test.exe') "
               f"-Destination '{remote}'", timeout=120)
        r = box.ps(f"& '{remote}'; Write-Output ('EXITCODE=' + $LASTEXITCODE)", timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        ok = "EXITCODE=0" in out
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("FAIL:") or "checks," in line:
                print(f"      {line}")
        if not ok:
            print(f"    [iocp] toFinalPath unit test FAILED\n{out[-2000:]}")
        return ok
    finally:
        box.ps(f"Remove-Item -Force -LiteralPath '{remote}' -ErrorAction SilentlyContinue",
               timeout=120)
        shutil.rmtree(pathlib.Path(local_exe).parents[1], ignore_errors=True)


if __name__ == "__main__":
    sys.exit(0 if run(backends=[H.IOCP]) else 1)
