# Installing Tessera v1

**One line:**

```bash
curl -fsSL https://<your-repo>/dist/install.sh | bash
```

Replace `<your-repo>` with the canonical HTTPS location that serves the
release `dist/` directory (for example a GitHub release or a self-hosted
mirror). The installer is **authoritative-source driven**: every behavior
below is defined by **RFC-0013 — Distribution & Installation**
(`formal-specifications/rfcs/rfc-0013-distribution.md`). Where this guide and
RFC-0013 disagree, **RFC-0013 wins.**

> Tessera v1 is **not** on PyPI. There is no `pip install tessera`. The only
> supported install path is the checksum-pinned source tarball fetched by the
> command above.

---

## What the one-liner does

`install.sh` is the **only** thing `curl` fetches and pipes to `bash`. It is
intentionally tiny and auditable. It then hands off to a verified installer
that ships *inside* the tarball.

1. **Resolve the release.** The bootstrap hardcodes the release base URL, the
   pinned tag/version (e.g. `v1.0.0`), the exact tarball name
   (`tessera-<version>.tar.gz`), and an **inline SHA256** of that tarball.
2. **Download + verify.** It fetches the tarball and its `.sha256` sidecar,
   verifies the checksum, and **refuses (non-zero exit) on any mismatch**.
   The tarball is never extracted or executed before it passes.
3. **Extract** to a temporary directory.
4. **Hand off** to `install-modules/main.sh` inside the tarball, which runs,
   in order, with `set -euo pipefail` and a cleanup trap:

   | Module | Job |
   |--------|-----|
   | `00-precheck.sh` | Detect Python **≥ 3.12** (install `uv` via its checksum-verified installer and run `uv python install 3.12` if missing). Detect/require `uv`. **Linux-only** platform check (abort on non-Linux). |
   | `01-perms.sh` | User-scope permission stub. **No `sudo`** is ever invoked. |
   | `02-scaffold.sh` | Create `~/.local/share/tessera`. Run `uv pip install .` from the extracted source (non-editable, offline-resolvable from the vendored `uv.lock`). Symlink the CLI entry points. **No systemd unit is written** — `tessera repo init` generates it. |
   | `03-test.sh` | Run `e2e/smoke_test.py`. A **non-zero exit aborts the install** and triggers cleanup. |
   | `04-cleanup.sh` | Remove the tarball, bootstrap script, extraction dir, E2E artifacts, and temp repos. **Never removes the installed application.** |

---

## User-scope default (no sudo, ever)

The install is **entirely within your home directory**. Nothing is written
outside `~`, and `sudo` is never invoked — this removes the largest class of
install-time privilege-escalation risk.

| Artifact | Path |
| --- | --- |
| Install prefix (app) | `~/.local/share/tessera/` |
| Runtime systemd unit (non-template, written by `tessera repo init`) | `~/.config/systemd/user/tessera-runtime.service` |
| CLI entry — `tessera` | `~/.local/bin/tessera` (symlink) |
| CLI entry — `ticket` | `~/.local/bin/ticket` (symlink) |

`~/.local/bin` must be on `PATH` (standard on most Linux distributions).
Both `tessera` (the runtime CLI) and `ticket` (the thin SDK client CLI) are
symlinked there.

The install prefix under the share dir is named **`tessera/`**, matching the
two-package source layout:

- `src/tessera_runtime/` — the engine (`tessera`/`ticket` CLI, daemon,
  `runtime.sock` RPC server).
- `src/tessera_sdk/` — the client SDK (`import tessera_sdk`).

---

## systemd user unit

The runtime is supervised by a **non-template** systemd user unit,
`~/.config/systemd/user/tessera-runtime.service`. Its `WorkingDirectory=` and
`TESSERA_REPO=` are **hardcoded to the canonical repo path** (the prefix's
`TicketsRepository/`) by `tessera repo init`.

> **dev/v1 change:** the unit is no longer written by the installer. The
> first `tessera repo init` (or an explicit `tessera runtime enable`) generates
> it. This is an experimental branch and is not part of a release tarball yet.

Bring it up after init:

```bash
systemctl --user daemon-reload
systemctl --user enable --now tessera-runtime.service
journalctl --user -u tessera-runtime.service -f
```

(Enable lingering with `loginctl enable-linger $USER` if you want it to run
while you are logged out.)

---

## Post-install E2E

After scaffolding, `03-test.sh` runs `e2e/smoke_test.py`, which asserts:

- A lifecycle/event **hook fired** during a minimal runtime start.
- The environment variable **`TESSERA_TICKET_ID`** is set (proves the
  SDK → runtime path is wired).

If the smoke test returns non-zero, the installer **aborts** and
`04-cleanup.sh` tears down the partial install — you are left with **no
half-installed `tessera` on `PATH`**. Re-run the one-liner once you have
resolved the environment issue.

---

## Clean-box requirement

The installer targets a **clean Linux box**:

- **Linux only.** Any non-Linux OS aborts in `00-precheck.sh` with a clear
  message. No macOS/Windows fallbacks exist in v1 (Charter §7 Non-Goal).
- A working `curl` and, ideally, TLS network access to the release host.
- `~/.local/bin` on `PATH`. If a previous, broken `tessera`/`ticket` is
  already on `PATH`, remove it first — reinstalls are **idempotent** and will
  overwrite the prefix, but a conflicting manual install is not cleaned for
  you.

The install is a **closed, non-editable artifact** (no `-e`/`pip install -e`),
so it never links back to a checkout. Re-running the bootstrap with a newer
pinned tag is a safe, deterministic **reinstall** into the same prefix.

---

## Useful flags

| Flag | Effect |
| --- | --- |
| `--check` | Verify the download + checksum only; do not install. |
| `--dry-run` | Run prechecks and print planned actions without mutating the system. |

```bash
curl -fsSL https://<your-repo>/dist/install.sh | bash -s -- --check
curl -fsSL https://<your-repo>/dist/install.sh | bash -s -- --dry-run
```

---

## Verification after install

```bash
which tessera ticket          # both resolve under ~/.local/bin
tessera --help                # runtime CLI
ticket  --help                # SDK client CLI
```

If either reports "command not found", confirm `~/.local/bin` is on `PATH`
and re-run the one-liner (it is idempotent).

---

## Uninstall

`04-cleanup.sh` only removes **temporary** artifacts (tarball, bootstrap
script, extraction dir, E2E outputs). It never deletes the installed app. To
fully remove Tessera v1 from a box, delete the user-scoped artifacts:

```bash
systemctl --user disable --now tessera-runtime.service 2>/dev/null
rm -rf ~/.local/share/tessera          # prefix: repo, venv, zfunc completions
rm -f  ~/.local/bin/tessera ~/.local/bin/ticket
rm -f  ~/.config/systemd/user/tessera-runtime.service
rm -f  ~/.zfunc/_tessera                # only if you used typer's own installer
systemctl --user daemon-reload
# If a pre-dev/v1 stray runtime dir was left at home, remove it explicitly:
#   tessera repo clean      # safe: refuses if a runtime looks live there
```

---

## Manual Installation (build from source)

Use this path when you are installing from **`dev/v1`** (or any development
branch). `dev/v1` does **not** ship an official release, so the `curl … | bash`
one-liner above does not apply to it — that bootstrap is pinned to the `v1.0.0`
tag and release host published from `main`.

Instead you build the release artifact **locally** and install from it, using
the same installer the production path uses.

> **You do not need to disable checksum verification.** `make dist` rebuilds the
> tarball *and* injects that tarball's real SHA256 into your local
> `dist/install.sh`. Verification then passes against **your own** artifact. This
> is deliberately better than bypassing it: you keep the integrity check that
> catches a truncated or corrupted local build (RFC-0013 §7).

### Prerequisites

- **Linux** — enforced by `00-precheck.sh` (Charter §7 Non-Goal; no macOS/Windows).
- **`uv`** — install via `curl -LsSf https://astral.sh/uv/install.sh | sh` if missing.
  (`00-precheck.sh` will install it for you if absent.)
- **Python ≥ 3.12** — `00-precheck.sh` provisions one via `uv python install 3.12`
  if your system has none.
- **`git`**, **`curl`**, **`tar`**, and **`make`**.

### Step 1 — Clone and check out the branch

```bash
git clone https://github.com/kazkriska/tessera.git
cd tessera
git checkout dev/v1
```

### Step 2 — Build the distribution

```bash
make dist
```

This runs `scripts/build_dist.py build`, which:

1. Stages the payload under `dist/` from the repo sources of truth: `src/`,
   `pyproject.toml`, `uv.lock`, `LICENSE`, `e2e/smoke_test.py`, plus the
   hand-authored installer copied from `release/` (`install.sh`,
   `install-modules/`, `README.md`). `dist/` is a **generated, git-ignored**
   build output — none of it is committed.
2. Derives the tarball payload from the explicit inclusion set
   (`_PAYLOAD_INCLUDE` in `scripts/build_dist.py`: `src/`, `pyproject.toml`,
   `uv.lock`, `LICENSE`, `README.md`, `install-modules/`, `e2e/`) filtered by
   `.distignore`. `dist/SOURCE_MANIFEST.txt` is **generated** by this step as a
   verification artifact (output only — never an input).
3. Writes `dist/tessera-<version>.tar.gz` and `dist/tessera-<version>.tar.gz.sha256`.
4. **Injects the real SHA256 into `dist/install.sh`**, replacing the build placeholder.

Confirm the staging directory is self-consistent before installing:

```bash
make check-dist
# [build_dist] check: dist/ is consistent (sha 3476e106ea91…)
```

The digest differs on every rebuild — gzip embeds timestamps, so the tarball is
not byte-reproducible across runs. Only its **self-consistency** with
`dist/install.sh` matters here.

If this reports `dist/ is STALE`, re-run `make dist` — see
[Adding new modules](#adding-new-modules) if it persists.

### Step 3 — Install

Two supported routes. **Route A** is recommended: it is the exact production
code path, just pointed at a local artifact.

#### Route A — full installer against your local tarball (recommended)

`TESSERA_TARBALL` makes the bootstrap use a local file instead of downloading
from the release host. The checksum is **still verified**, against the digest
`make dist` just injected.

```bash
TESSERA_TARBALL="$PWD/dist/tessera-1.0.0.tar.gz" bash dist/install.sh
```

You get the complete pipeline — fetch → verify → extract → modules `00`–`04`:

```
==> [tessera] phase 2/4 · verify sha256
    ok: sha256 verified: 3476e106ea91…
==> [tessera] phase 4/4 · running install modules
    ok: packages installed (tessera_runtime, tessera_sdk)
==> [tessera] install complete
```

#### Route B — run the install modules directly (fastest iteration)

`install-modules/main.sh` runs against an unpacked source tree, skipping the
fetch, extract and checksum phases entirely. It resolves `TESSERA_SRC_DIR` to
its own parent directory, so run it from `dist/`:

```bash
bash dist/install-modules/main.sh
```

Use this when you are rebuilding repeatedly and do not need to exercise the
tarball path. Note it installs from `dist/`, so **run `make dist` first** to pick
up your latest `src/` changes.

### Sandboxing the install

To avoid clobbering an existing installation, redirect the prefix and bin dir:

```bash
export TESSERA_PREFIX="$HOME/.local/share/tessera-dev"
export TESSERA_BIN_DIR="$HOME/.local/bin-dev"
TESSERA_TARBALL="$PWD/dist/tessera-1.0.0.tar.gz" bash dist/install.sh
```

> **These variables only affect the installer shell, not the runtime.** The
> `tessera` CLI hardcodes `DEFAULT_PREFIX = ~/.local/share/tessera`
> (`repo.py`) and `SYSTEMD_USER_DIR = ~/.config/systemd/user`
> (`systemd_units.py`). A no-argument `tessera repo init` will therefore still
> write to your **real** home, regardless of `TESSERA_PREFIX`. Always pass an
> explicit path — `tessera repo init /path/to/scratch` — when testing.

### Environment variables

| Variable | Effect |
| --- | --- |
| `TESSERA_TARBALL` | Install from this local tarball instead of downloading. Still SHA256-verified. |
| `TESSERA_PREFIX` | Install prefix (default `~/.local/share/tessera`). Installer only. |
| `TESSERA_BIN_DIR` | Symlink target dir (default `~/.local/bin`). Installer only. |
| `TESSERA_SYSTEMD_USER_DIR` | Unit dir used by the installer (default `~/.config/systemd/user`). |
| `TESSERA_SKIP_E2E=1` | Skip the post-install smoke test in `03-test.sh`. Use it to speed up iterative rebuilds. A full install (smoke test run) is safe and recommended before trusting a build — the smoke test is idempotent and tolerates a runtime that `repo init` already started. |
| `TESSERA_SRC_DIR` | Source tree for `main.sh` when invoked directly (auto-resolved). |

> **Run the smoke test at least once before trusting a build.** The smoke test's
> first step is `tessera repo init`, which is exactly what catches a payload with
> missing modules. On `dev/v1` `repo init` auto-starts the daemon, so the smoke
> test detects an already-running runtime and leaves it alone rather than failing
> — you get a real end-to-end verification without the install aborting.

### Verify the install

```bash
tessera --help                 # runtime CLI responds
ticket  --help                 # SDK client CLI responds
tessera repo init /tmp/scratch-repo   # the real integration check
```

`tessera --help` succeeding is **not** sufficient — a payload missing runtime
modules still passes it, because the imports are lazy and inside the command
bodies. `tessera repo init` imports `tessera_runtime.systemd_units` and is the
first command to fail on an incomplete build. Confirm the packaged modules
directly if you want certainty:

```bash
"$TESSERA_PREFIX/venv/bin/python" -c \
  "import tessera_runtime.daemon, tessera_runtime.systemd_units, tessera_sdk; print('ok')"
```

> **Post-install e2e smoke test (step 5/6).** The full install runs
> `e2e/smoke_test.py`, which exercises the `on_state_updated` hook. The earlier
> `dev/v1` defect where this hook stayed silent for tickets created right after
> `repo init` (the daemon registered inotify watches before any ticket existed,
> and never re-scanned) is **fixed** — `FsWatcher` now self-heals via
> `_watch_root()` on new `*.ticket` dirs (commit `4108ad4`, GitHub #3, merged to
> `dev/v1`). After a rebuild, the hook should fire and the smoke test should pass.
> If it still fails, the authoritative payload-integrity gate is the import
> check below — confirm the packaged modules import before trusting the build.

### Adding new modules

The tarball payload is **derived automatically** from the source tree — you no
longer maintain a manifest by hand. `scripts/build_dist.py` walks the inclusion
set (`_PAYLOAD_INCLUDE`: `src/`, `pyproject.toml`, `uv.lock`, `LICENSE`,
`README.md`, `install-modules/`, `e2e/`) and drops anything matched by
`.distignore`. A new module under `src/` is therefore picked up on the very next
`make dist` with **no manifest edit**.

To make that guarantee enforceable, `make check-dist` runs a **completeness
gate**: every file under `src/` not excluded by `.distignore` must be present in
the tarball, or the build hard-fails. This is the regression guard that catches
an omitted module at CI time rather than as a runtime `ModuleNotFoundError`.

**Workflow when you add a module under `src/`:**

```bash
make dist
tar -tzf dist/tessera-1.0.0.tar.gz | grep your_new_module.py   # confirm it shipped
make check-dist                                                   # gate passes
```

If a source file genuinely must not ship, add a pattern to `.distignore`
(the gate respects it); otherwise the gate will refuse the build.

> `dist/SOURCE_MANIFEST.txt` still exists but is now **generated** by `make dist`
> (it lists the exact files shipped, so `sha256sum -c` can verify them). Do not
> edit it by hand — it is output only.

### Pitfalls

- **`dist/` is a generated, git-ignored build output — never commit it.**
  `make dist` rewrites the tarball, its `.sha256`, and the `EXPECTED_SHA256`
  line in `dist/install.sh`. Those artifacts are produced locally for dev/test
  installs; the release process on `main` rebuilds and publishes them. `.gitignore`
  already excludes `dist/`, so `git status` will not show it — that is expected.
- **The installer writes to your real home even when sandboxed.** `02-scaffold.sh`
  runs `tessera completion install`, which creates
  `~/.local/share/tessera/zfunc/_tessera` and appends an `fpath+=` block to
  `~/.zshrc` — both at the hardcoded default prefix.
- **Editable installs are forbidden.** RFC-0013 §3 requires a closed, non-editable
  artifact; do not substitute `uv pip install -e .`.
- **Never use `sudo`.** `01-perms.sh` refuses to run as root.
- **Reinstalls are idempotent.** `uv venv --clear` recreates the venv, so
  re-running either route safely overwrites the previous install.

### Uninstall

```bash
systemctl --user disable --now tessera-runtime.service 2>/dev/null
rm -rf "${TESSERA_PREFIX:-$HOME/.local/share/tessera}"
rm -f  "${TESSERA_BIN_DIR:-$HOME/.local/bin}"/tessera "${TESSERA_BIN_DIR:-$HOME/.local/bin}"/ticket
rm -f  ~/.config/systemd/user/tessera-runtime.service
systemctl --user daemon-reload
# remove the completion block appended to ~/.zshrc, if present
```

---

## Reference

- **Authoritative spec:** `formal-specifications/rfcs/rfc-0013-distribution.md`
  (Ratified, v1.0). This INSTALL.md is a human-readable summary of RFC-0013.
- Future (v1.1): signed tarballs (GPG / sigstore) and an opt-in `--system`
  mode (the only place `sudo` may appear). Neither is in v1.0.
