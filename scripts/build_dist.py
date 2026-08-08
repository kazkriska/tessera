#!/usr/bin/env python3
"""
build_dist.py — the single source of truth for the Tessera v1 release artifact.

The framework repo root holds the *source of truth*:
    src/                runtime engine + SDK (installed packages)
    pyproject.toml      packaging (version lives here)
    uv.lock             pinned dependencies
    LICENSE             project license
    e2e/smoke_test.py   curated end-to-end smoke test
    dist/install.sh, dist/install-modules/   installer (hand-authored)
    dist/README.md      user-facing README (hand-authored, differs from root)
    dist/SOURCE_MANIFEST.txt   list of files shipped in the tarball

This script REGENERATES everything under dist/ that is derived:
    dist/src/           <- copy of src/ (minus caches)
    dist/pyproject.toml <- copy of pyproject.toml
    dist/uv.lock        <- copy of uv.lock
    dist/LICENSE        <- copy of LICENSE
    dist/e2e/smoke_test.py <- copy of e2e/smoke_test.py

It then assembles dist/tessera-<version>.tar.gz from the SOURCE_MANIFEST
entries (plus the installer) and writes dist/tessera-<version>.tar.gz.sha256.

Finally it injects the real SHA256 into dist/install.sh (replacing the
build placeholder), so the shipped bootstrap verifies against the real tarball.

Modes:
    build     regenerate dist/ + tarball + sha + install.sh digest (default)
    check     verify dist/ matches sources (no drift) WITHOUT writing;
              exit non-zero if dist/ is stale (used by CI)

RFC-0013 is the authoritative spec for the manifest and installer behavior.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION_RE = re.compile(r'^version\s*=\s*"(?P<v>[^"]+)"', re.MULTILINE)
PLACEHOLDER = "REPLACE_WITH_REAL_SHA256"
SHA_LINE_RE = re.compile(r'^EXPECTED_SHA256=.*$', re.MULTILINE)


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[build_dist] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_version() -> str:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        die("pyproject.toml not found at repo root")
    m = VERSION_RE.search(pyproject.read_text())
    if not m:
        die("could not find version= in pyproject.toml")
    return m.group("v")


# (source-of-truth path, destination-under-dist, copy_as_dir?)
DERIVED = [
    ("src", "src", True),
    ("pyproject.toml", "pyproject.toml", False),
    ("uv.lock", "uv.lock", False),
    ("LICENSE", "LICENSE", False),
    ("e2e/smoke_test.py", "e2e/smoke_test.py", False),
]

# Files that are hand-authored under dist/ and never regenerated.
HAND_AUTHORED = [
    "README.md",
    "SOURCE_MANIFEST.txt",
    "install.sh",
    "install-modules",
]


def _is_cache(p: Path) -> bool:
    parts = set(p.parts)
    return "__pycache__" in parts or p.name.endswith(".pyc")


def _sync_derived(write: bool) -> list[str]:
    """Copy source-of-truth -> dist/. Returns list of changed paths (if write)."""
    changed = []
    for src_rel, dst_rel, as_dir in DERIVED:
        src = ROOT / src_rel
        dst = DIST / dst_rel
        if not src.exists():
            die(f"source of truth missing: {src_rel}")
        if as_dir:
            # remove stale dst dir then copy fresh (avoids orphan files)
            if dst.exists():
                if write:
                    shutil.rmtree(dst)
                else:
                    # check mode: compare contents
                    if _tree_diff(src, dst):
                        changed.append(dst_rel)
                    continue
            if write:
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                changed.append(dst_rel)
            else:
                if _tree_diff(src, dst):
                    changed.append(dst_rel)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if write:
                shutil.copy2(src, dst)
                changed.append(dst_rel)
            else:
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    changed.append(dst_rel)
    return changed


def _tree_diff(a: Path, b: Path) -> bool:
    """Return True if trees a and b differ (ignoring __pycache__)."""
    a_files = {p.relative_to(a) for p in a.rglob("*") if p.is_file() and not _is_cache(p)}
    b_files = {p.relative_to(b) for p in b.rglob("*") if p.is_file() and not _is_cache(p)}
    if a_files != b_files:
        return True
    for rel in a_files:
        if (a / rel).read_bytes() != (b / rel).read_bytes():
            return True
    return False


def _manifest_entries() -> list[str]:
    manifest = DIST / "SOURCE_MANIFEST.txt"
    if not manifest.exists():
        die("dist/SOURCE_MANIFEST.txt missing")
    entries = []
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # format: <sha>  <relpath>
        rel = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else line
        entries.append(rel)
    return entries


def _build_tarball(version: str, write: bool) -> tuple[str, bytes] | tuple[str, None]:
    entries = _manifest_entries()
    # installer is always shipped alongside the manifest entries
    installer = ["install.sh", "install-modules"]
    tarball_name = f"tessera-{version}.tar.gz"
    tarball = DIST / tarball_name
    sha_path = DIST / f"{tarball_name}.sha256"

    if not write:
        # check mode: confirm tarball + sha exist and are consistent
        if not tarball.exists() or not sha_path.exists():
            return tarball_name, None
        return tarball_name, tarball.read_bytes()

    # assemble tarball from manifest entries + installer, rooted at dist/
    cmd = ["tar", "-czf", str(tarball), "-C", str(DIST), *entries, *installer]
    subprocess.run(cmd, check=True)
    data = tarball.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    sha_path.write_text(f"{sha}  {tarball_name}\n")
    return tarball_name, data


def _inject_sha(write: bool, sha: str | None) -> bool:
    install_sh = DIST / "install.sh"
    if not install_sh.exists():
        die("dist/install.sh missing")
    text = install_sh.read_text()
    if sha is None:
        # check mode: is the embedded digest the real one (not placeholder)?
        m = re.search(r'EXPECTED_SHA256="(?P<d>[^"]+)"', text)
        if not m:
            return True
        return m.group("d") == PLACEHOLDER  # True == "still placeholder" == drift
    if write:
        new = SHA_LINE_RE.sub(f'EXPECTED_SHA256="{sha}"', text)
        install_sh.write_text(new)
        return False
    return False


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "build"
    if mode not in ("build", "check"):
        die(f"unknown mode: {mode} (use 'build' or 'check')")
    write = mode == "build"

    version = read_version()
    print(f"[build_dist] version: {version}")

    changed = _sync_derived(write)
    if mode == "check" and changed:
        die(f"dist/ is STALE — derived files differ from sources: {changed}\n"
            f"Run `make dist` (or `python scripts/build_dist.py build`) to regenerate.")

    tarball_name, data = _build_tarball(version, write)
    if mode == "check":
        if data is None:
            die("dist/ tarball or .sha256 missing — run `make dist`.")
        sha = hashlib.sha256(data).hexdigest()
        if _inject_sha(False, sha):
            die("dist/install.sh still carries the build placeholder SHA256 — run `make dist`.")
        # verify manifest sha matches tarball
        sha_file = DIST / f"{tarball_name}.sha256"
        on_disk = sha_file.read_text().split()[0] if sha_file.exists() else None
        if on_disk != sha:
            die("dist/ .sha256 does not match the tarball — run `make dist`.")
        print(f"[build_dist] check: dist/ is consistent (sha {sha[:12]}…)")
        return

    # build mode
    sha = hashlib.sha256(data).hexdigest()
    _inject_sha(True, sha)
    print(f"[build_dist] built {tarball_name} (sha {sha[:12]}…)")
    print(f"[build_dist] wrote {tarball_name}.sha256")
    print(f"[build_dist] injected SHA256 into dist/install.sh")
    if changed:
        print(f"[build_dist] regenerated derived: {changed}")


if __name__ == "__main__":
    main()
