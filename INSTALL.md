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
   | `02-scaffold.sh` | Create `~/.local/share/tessera`. Run `uv pip install .` from the extracted source (non-editable, offline-resolvable from the vendored `uv.lock`). Register the systemd user unit template. Symlink the CLI entry points. |
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
| Runtime systemd unit (template) | `~/.config/systemd/user/tessera-runtime@.service` |
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

## Reference

- **Authoritative spec:** `formal-specifications/rfcs/rfc-0013-distribution.md`
  (Ratified, v1.0). This INSTALL.md is a human-readable summary of RFC-0013.
- Future (v1.1): signed tarballs (GPG / sigstore) and an opt-in `--system`
  mode (the only place `sudo` may appear). Neither is in v1.0.
