# Installing Tessera v1

One command. No PyPI, no package index, no `sudo`.

```bash
curl -fsSL https://github.com/kazkriska/tessera-v1/releases/download/v1.0.0/install.sh | bash
```

When it finishes you have `tessera` and `ticket` on your `PATH`:

```bash
tessera --help
tessera repo init .
```

## What it does

The bootstrap script is deliberately tiny and auditable — pipe it to `less`
first if you want to read it before running it. In order, it:

1. **Downloads** the pinned release tarball `tessera-1.0.0.tar.gz` and its
   `.sha256` sidecar over HTTPS.
2. **Verifies the SHA256** against a checksum embedded in the script itself.
   A mismatch is a hard refusal — nothing is extracted or executed.
3. **Extracts** to a temporary directory and hands off to the verified
   installer modules shipped inside the tarball.
4. **Installs** the app to the prefix `tessera/` under your home —
   `~/.local/share/tessera` — via `uv pip install .` from the extracted
   source (a closed, non-editable artifact resolved against the vendored
   `uv.lock`).
5. **Registers** the systemd user unit template
   `~/.config/systemd/user/tessera-runtime@.service`.
6. **Symlinks** `~/.local/bin/tessera` and `~/.local/bin/ticket` to the
   installed entry points.
7. **Runs a post-install E2E smoke test** — it asserts a lifecycle hook fires
   and that `TESSERA_TICKET_ID` is set in the hook environment. If the test
   fails the installer aborts and rolls the partial install back, so you are
   never left with a half-installed `tessera`.
8. **Cleans up** the tarball, bootstrap, extraction directory, and test
   artifacts. It never removes the installed application.

Two flags are supported if you want to look before you leap:

```bash
curl -fsSL <url>/install.sh | bash -s -- --check     # download + verify checksum only
curl -fsSL <url>/install.sh | bash -s -- --dry-run   # prechecks + planned actions, no changes
```

## Requirements

- **Linux.** macOS and Windows are out of scope for v1; the installer detects
  the platform and aborts with a clear message on anything else.
- **`curl`** and network access to the release URL over TLS.
- **Python ≥ 3.12 and `uv`** — *provided automatically if missing.* The
  precheck module installs `uv` (official, checksum-verified installer) and
  runs `uv python install 3.12` when no suitable Python is found. A clean box
  with nothing but `curl` is a supported starting point.

## Where things go

| Artifact | Path |
| --- | --- |
| Install prefix (app) | `~/.local/share/tessera/` |
| systemd user unit (template) | `~/.config/systemd/user/tessera-runtime@.service` |
| CLI entry — `tessera` | `~/.local/bin/tessera` (symlink) |
| CLI entry — `ticket` | `~/.local/bin/ticket` (symlink) |

Nothing is written outside your home directory. `~/.local/bin` must be on
your `PATH` (it is by default on most Linux distributions).

## Notes

- **User scope, no `sudo`.** Every action stays inside your home; the
  installer never invokes `sudo` and never touches system directories.
- **Per-repo daemon.** The runtime is registered as a *systemd template* unit,
  so each ticket repository gets its own instance:

  ```bash
  systemctl --user start tessera-runtime@<instance>
  systemctl --user status tessera-runtime@<instance>
  ```

- **Upgrades are idempotent reinstalls.** Re-run the one-liner with a newer
  pinned tag; it re-extracts and reinstalls into the same prefix. The install
  is a closed artifact, so there is no side-by-side version state to
  reconcile.
- **Uninstall:**

  ```bash
  systemctl --user disable --now 'tessera-runtime@*'
  rm -rf ~/.local/share/tessera
  rm -f ~/.local/bin/tessera ~/.local/bin/ticket
  rm -f ~/.config/systemd/user/tessera-runtime@.service
  ```

  Your `TicketsRepository/` data is never touched by install or uninstall.

## Installing from a checkout instead

Contributors working from a clone should use the development path in
[README.md](README.md#development) (`uv venv && uv pip install -e ".[dev]"`).
Editable installs are deliberately **not** what the user installer produces.

---

**Authoritative spec:** `formal-specifications/rfcs/rfc-0013-distribution.md`.
Where this guide and RFC-0013 disagree, the RFC wins.
