#!/usr/bin/env python3
"""#HIGH crash (2026-07-02 coverage review, reproduced): a SOURCE argument containing '*' is treated
as a wildcard by ScanFileOrFolder::parseWildcardSources, which rewrites the component into a regex and
feeds it to std::regex. A storable filename that ALSO contains a regex metacharacter -- an unbalanced
'(' or '[' -- made std::regex THROW std::regex_error which, uncaught in the scan thread, called
std::terminate() and ABORTED THE WHOLE APP (SIGABRT). Reproduced: `cp '<dir>/img(*.jpg' <dst>` ->
`terminate called after throwing 'std::regex_error': Mismatched '(' and ')'` -> signal 6.

FIX (no try/catch -- Ultracopier builds with -fno-exceptions on some targets): build the match regex by
ESCAPING every regex metacharacter and turning only '*' into [^/\\]*, so the pattern is ALWAYS valid and
std::regex can never throw. This also gives correct shell-glob semantics ('(', '.', ... now match
literally instead of leaking through as regex).

ASSERTS (async + io_uring): the engine STAYS ALIVE (no crash) when given a wildcard source whose name
carries hostile regex bytes, AND the wildcard resolves with literal-metachar semantics -- 'img(*.jpg'
matches 'img(good.jpg' (copied) but NOT 'imgXgood.jpg' (the '(' is literal, not "any char"). RED
before the fix (SIGABRT / stayed_alive False); GREEN after.
"""
import sys, os, pathlib, shutil, hashlib
_CASES_DIR = str(pathlib.Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if p not in ("", _CASES_DIR)]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

GOOD = b"the-glob-must-match-and-copy-this\n"
DECOY = b"the-literal-paren-must-NOT-match-this\n"


def _one(backend):
    root = K.fresh_src_root(f"weirdsrc_{backend}")
    shutil.rmtree(root, ignore_errors=True)
    src = os.path.join(root, "src")
    dst = K.fresh_dest(f"weirdsrc_dst_{backend}")
    os.makedirs(src)
    # 'img(good.jpg' -> a valid regex made from 'img(*.jpg' (unbalanced '(' -> the CRASH trigger) must
    # match it literally; 'imgXgood.jpg' proves the '(' is LITERAL, not a regex "(" group / any-char.
    K.write_file(os.path.join(src, "img(good.jpg"), GOOD)
    K.write_file(os.path.join(src, "imgXgood.jpg"), DECOY)
    hostile_arg = os.path.join(src, "img(*.jpg")   # '*' glob + unbalanced '(' regex metachar
    r = H.run(backend, "cp", [hostile_arg], dst,
              file_collision=H.FileCollision.OVERWRITE, folder_collision=H.FolderCollision.MERGE,
              expect_dir=None)
    copied = os.path.join(dst, "img(good.jpg")
    decoy_copied = os.path.join(dst, "imgXgood.jpg")
    good_ok = os.path.exists(copied) and hashlib.sha256(K.read_file(copied)).hexdigest() == hashlib.sha256(GOOD).hexdigest()
    decoy_absent = not os.path.exists(decoy_copied)   # '(' is literal -> imgXgood.jpg must NOT match
    ok = r.stayed_alive and r.completed and not r.oom_killed and r.mem_errors == 0 and good_ok and decoy_absent
    note = (f"{backend}: alive={r.stayed_alive} completed={r.completed} matched_good={good_ok} "
            f"literal_paren={decoy_absent} mem_err={r.mem_errors}")
    if not r.stayed_alive:
        note += "  *** CRASH: a '*'+regex-metachar source arg aborted the engine (uncaught std::regex_error) ***"
    print(f"      [{backend}] {note}")
    shutil.rmtree(root, ignore_errors=True)
    return ok


def run(backends=None, memcheck=H.NONE) -> bool:
    bes = [b for b in (backends or [H.ASYNC]) if b in (H.ASYNC, H.IO_URING)]
    if not bes:
        print("    [weird_source_arg] SKIP (async/io_uring only)")
        return True
    ok = all(_one(b) for b in bes)
    print(f"    [weird_source_arg] {'PASS  (hostile wildcard source arg: no crash, literal-metachar glob)' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
