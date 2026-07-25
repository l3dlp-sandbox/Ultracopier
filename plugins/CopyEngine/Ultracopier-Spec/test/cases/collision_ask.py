#!/usr/bin/env python3
"""The SHIPPING DEFAULT collision policies -- fileCollision=0 and folderCollision=0, "Ask" --
driven against REAL collisions.

Why this case exists: index 0 is what every user runs by default (CopyEngineFactory.cpp), and
"copy onto files that already exist" is the single most common real-world scenario. Until the
collision dialogs got a test seam (FileExistsDialog/FolderExistsDialog/FileIsSameDialog
::overrideFactory, mirroring FileErrorDialog), an Ask popped a modal nothing could answer, so a
headless run WEDGED and no case could cover the default at all. hooks/CollisionDialogHook.cpp now
catches EVERY Ask and answers it from the env, so this case asserts three things per sub-case:

  1. the job COMPLETES (no wedge on the default policy -- the regression that would hang a user),
  2. the answered action is really APPLIED (overwrite replaces, skip keeps the stale bytes,
     merge keeps both trees),
  3. the dialog was really SHOWN (the hook's marker file proves the Ask path was taken, so a
     future change that silently stops asking cannot make this case vacuously pass).
"""
import sys, pathlib, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from lib import harness as H
from lib import casekit as K

STALE = b"STALE PRE-EXISTING CONTENT\n"
SRCTXT = b"hello ultracopier\n"


def _marker_path(tag):
    p = os.path.join(K.fresh_src_root("ask_marker_" + tag), "marker.txt")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        os.remove(p)
    return p


def _marker_kinds(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def _mktree(tag):
    """Minimal source: one file that will collide, one that will not."""
    src = K.fresh_src_root(tag)
    K.write_file(os.path.join(src, "collide.txt"), SRCTXT)
    K.write_file(os.path.join(src, "fresh.txt"), SRCTXT)
    return src


def _file_collision(backend, answer, expect_overwritten):
    """fileCollision=ASK against a real file collision, answered `answer`."""
    tag = f"ask_file_{answer}"
    src = _mktree(tag + "_src")
    dest = K.fresh_dest(tag + "_dest")
    base = os.path.basename(src)
    collide = os.path.join(dest, base, "collide.txt")
    K.write_file(collide, STALE)

    marker = _marker_path(tag)
    os.environ["ULTRACOPIER_TEST_COLLISION_MARKER"] = marker
    os.environ["ULTRACOPIER_TEST_FILE_COLLISION_ACTION"] = answer
    try:
        r = H.run(backend, "cp", [src], dest,
                  file_collision=H.FileCollision.ASK,          # <- the shipping default
                  folder_collision=H.FolderCollision.MERGE)
    finally:
        os.environ.pop("ULTRACOPIER_TEST_COLLISION_MARKER", None)
        os.environ.pop("ULTRACOPIER_TEST_FILE_COLLISION_ACTION", None)

    final = K.read_file(collide) if os.path.exists(collide) else b""
    applied = (final == SRCTXT) if expect_overwritten else (final == STALE)
    # the non-colliding sibling must land either way -- an Ask on one file must not drop the rest
    sibling = os.path.join(dest, base, "fresh.txt")
    sibling_ok = os.path.exists(sibling) and K.read_file(sibling) == SRCTXT
    asked = "file_collision" in _marker_kinds(marker)
    ok = r.completed and r.stayed_alive and applied and sibling_ok and asked
    if not ok:
        print(f"      [file/{answer}] completed={r.completed} alive={r.stayed_alive} "
              f"applied={applied} sibling={sibling_ok} dialog_shown={asked} "
              f"final={final!r}")
    return ok


def _folder_collision(backend, answer):
    """folderCollision=ASK against a real folder collision, answered `answer` (merge)."""
    tag = f"ask_folder_{answer}"
    src = K.fresh_src_root(tag + "_src")
    K.write_file(os.path.join(src, "sub", "from_source.txt"), SRCTXT)
    dest = K.fresh_dest(tag + "_dest")
    base = os.path.basename(src)
    # pre-existing destination FOLDER with different content -> a real folder collision
    K.write_file(os.path.join(dest, base, "sub", "pre_existing.txt"), STALE)

    marker = _marker_path(tag)
    os.environ["ULTRACOPIER_TEST_COLLISION_MARKER"] = marker
    os.environ["ULTRACOPIER_TEST_FOLDER_COLLISION_ACTION"] = answer
    try:
        r = H.run(backend, "cp", [src], dest,
                  file_collision=H.FileCollision.OVERWRITE,
                  folder_collision=H.FolderCollision.ASK)     # <- the shipping default
    finally:
        os.environ.pop("ULTRACOPIER_TEST_COLLISION_MARKER", None)
        os.environ.pop("ULTRACOPIER_TEST_FOLDER_COLLISION_ACTION", None)

    copied = os.path.join(dest, base, "sub")
    from_src = os.path.join(copied, "from_source.txt")
    pre = os.path.join(copied, "pre_existing.txt")
    merged = (os.path.exists(from_src) and K.read_file(from_src) == SRCTXT
              and os.path.exists(pre) and K.read_file(pre) == STALE)
    # NB the folder dialog is only raised for a genuine folder collision; if the engine resolves
    # it without asking, `asked` is False and the case fails loudly rather than passing vacuously.
    asked = "folder_collision" in _marker_kinds(marker)
    ok = r.completed and r.stayed_alive and merged and asked
    if not ok:
        print(f"      [folder/{answer}] completed={r.completed} alive={r.stayed_alive} "
              f"merged={merged} dialog_shown={asked}")
    return ok


def _check(backend):
    ok = True
    # answered OVERWRITE -> the stale destination bytes are replaced
    ok &= _file_collision(backend, "overwrite", expect_overwritten=True)
    # answered SKIP -> the destination keeps its bytes, and the job still completes
    ok &= _file_collision(backend, "skip", expect_overwritten=False)
    # answered MERGE on a folder collision -> both trees survive
    ok &= _folder_collision(backend, "merge")
    return bool(ok)


def run(backends=None, memcheck=H.NONE) -> bool:
    return K.for_backends(backends, _check)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
