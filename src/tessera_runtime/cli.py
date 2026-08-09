"""Tessera CLI — Typer application (Part X, RFC-0010).

The CLI wraps the :class:`tessera.Runtime` client; there is no duplicated
logic (Part XI §4.4). Commands that need a live runtime (``action``,
``transition``) operate directly on the filesystem when no runtime is
running (Part X §8).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from tessera_runtime.config import RuntimeConfig, load_config

app = typer.Typer(
    name="tessera",
    help="Tessera v1 — file-system-native ticket runtime framework",
    no_args_is_help=True,
)
runtime_app = typer.Typer(help="Manage the runtime daemon.")
repo_app = typer.Typer(help="Repository operations.")
app.add_typer(runtime_app, name="runtime")
app.add_typer(repo_app, name="repo")


def _find_root(path: str | None) -> Path:
    from tessera_sdk import find_repo_root

    try:
        return find_repo_root(path)
    except RuntimeError:
        if path:
            return Path(path).resolve()
        return Path.cwd().resolve()


def _client(path: str | None, attach: bool = False):
    from tessera_sdk import Runtime

    root = _find_root(path)
    if attach:
        try:
            return Runtime.connect(repo=root), root
        except Exception:  # noqa: BLE001
            return Runtime.direct(repo=root), root
    return Runtime.direct(repo=root), root


# --------------------------------------------------------------------------- #
# runtime
# --------------------------------------------------------------------------- #
@runtime_app.command("start")
def runtime_start(
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Boot the runtime daemon (singleton via runtime.sock)."""
    from tessera_runtime.daemon import launch_runtime_daemon, runtime_is_live
    from tessera_runtime.runtime.server import runtime_socket_path

    root = _find_root(repo)
    sock_path = runtime_socket_path(root)
    if runtime_is_live(root):
        typer.echo(f"error: runtime already running at {sock_path}", err=True)
        raise typer.Exit(1)
    try:
        pid = launch_runtime_daemon(root)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Runtime started (pid {pid}, sock {sock_path})")


@runtime_app.command("stop")
def runtime_stop(
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Graceful shutdown of the runtime daemon."""
    root = _find_root(repo)
    from tessera_sdk import Runtime, RuntimeNotRunning

    try:
        client = Runtime.connect(repo=root)
        client._rpc("shutdown")
        client.close()
    except (RuntimeNotRunning, OSError):
        typer.echo("Runtime is not running.")
        raise typer.Exit(1)
    typer.echo("Runtime stopped.")


@runtime_app.command("status")
def runtime_status(
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Show whether the runtime daemon is up and how many tickets it sees."""
    root = _find_root(repo)
    from tessera_runtime.repo import RUNTIME_DIR_NAME, repo_init
    from tessera_runtime.runtime.server import runtime_socket_path

    sock = runtime_socket_path(root)
    running = sock.exists()
    typer.echo(f"root:    {root}")
    typer.echo(f"socket:  {sock}")
    typer.echo(f"running: {'yes' if running else 'no'}")
    if not running:
        return
    from tessera_sdk import Runtime

    try:
        client = Runtime.connect(repo=root)
        tickets = client.discover()
        typer.echo(f"tickets: {len(tickets)}")
        client.close()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"status probe failed: {exc}", err=True)


# --------------------------------------------------------------------------- #
# repo
# --------------------------------------------------------------------------- #
@repo_app.command("init")
def repo_init_cmd(
    path: Optional[str] = typer.Argument(None, help="Directory to scaffold"),
) -> None:
    """Scaffold TicketsRepository/ + .ticket-runtime/ and start the runtime.

    With no PATH, initializes the canonical prefix
    (~/.local/share/tessera) and writes a root marker so all ``tessera``
    commands discover it without a cwd walk. On completion the runtime
    daemon is started (via systemd when available, else a detached process).
    """
    from tessera_runtime.repo import DEFAULT_PREFIX, repo_init, write_root_marker
    from tessera_runtime.systemd_units import start_runtime

    root = Path(path).resolve() if path else DEFAULT_PREFIX
    typer.echo(f"→ initializing Tessera repository at {root}")
    try:
        repo_init(root)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"→ created directory tree {root / 'TicketsRepository' / '.ticket-runtime'}")

    marker = write_root_marker(root)
    typer.echo(f"→ wrote root marker {marker}")

    typer.echo("→ starting runtime (services, sockets, watchers)…")
    status = start_runtime(root)
    typer.echo(f"→ {status}")
    typer.echo(f"Initialized Tessera repository at {root}")


@runtime_app.command("enable")
def runtime_enable(
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Write + enable the systemd user unit (no re-init; assumes repo exists)."""
    from tessera_runtime.systemd_units import write_unit

    root = _find_root(repo)
    unit_path = write_unit(root)
    reload_ = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if reload_.returncode != 0:
        typer.echo("warn: systemctl --user daemon-reload failed (no user session bus?)", err=True)
    en = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "tessera-runtime.service"],
        capture_output=True,
        text=True,
    )
    if en.returncode == 0:
        typer.echo(f"enabled + started {unit_path}")
    else:
        typer.echo(
            f"warn: systemd enable failed ({en.stderr.strip() or en.returncode}); "
            f"unit written to {unit_path} — start manually or use 'tessera runtime start'",
            err=True,
        )


@repo_app.command("scan")
def repo_scan(
    path: Optional[str] = typer.Argument(None, help="Framework root"),
) -> None:
    """Force rediscovery + registry rebuild."""
    from tessera_runtime.repo import rescan

    root = _find_root(path)
    result = rescan(root)
    count = len(result["registry"].list_all()) if result.get("registry") else 0
    typer.echo(f"Scanned {root}; {count} tickets in registry.")


@repo_app.command("clean")
def repo_clean() -> None:
    """Remove stray runtime state left directly under $HOME (~/.ticket-runtime).

    The runtime tree must live under a repository's
    ``TicketsRepository/.ticket-runtime/`` (or the canonical prefix). A bare
    ``~/.ticket-runtime/`` is leftover from pre-dev/v1 usage and is NOT where
    the daemon stores its socket. This removes it after confirming it is the
    stray home dir and not an active runtime.
    """
    from tessera_runtime.repo import stray_home_runtime_dir

    stray = stray_home_runtime_dir()
    if stray is None:
        typer.echo("no stray ~/.ticket-runtime found; nothing to clean")
        return
    # Refuse if a runtime appears live there (defensive — normal location is
    # inside a repo, so this should never be an active socket).
    sock = stray / "runtime.sock"
    if sock.exists():
        typer.echo(f"error: {sock} looks active — stop the runtime first", err=True)
        raise typer.Exit(1)
    import shutil

    shutil.rmtree(stray)
    typer.echo(f"removed stray runtime dir {stray}")


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #
completion_app = typer.Typer(help="Install shell completions.")
app.add_typer(completion_app, name="completion")


@completion_app.command("install")
def completion_install(
    shell: str = typer.Option("zsh", "--shell", help="Shell: zsh (bash planned)"),
) -> None:
    """Write the shell completion file under the prefix, not in $HOME.

    typer's own ``--install-completion`` drops ``~/.zfunc/_tessera`` in your
    home dir. To keep all Tessera state under the prefix, this writes the
    completion script to ``~/.local/share/tessera/zfunc/_tessera`` instead and
    prints the ``fpath`` line you add to your shell rc.
    """
    if shell != "zsh":
        typer.echo(f"error: only zsh supported on dev/v1 (got {shell!r})", err=True)
        raise typer.Exit(1)
    from tessera_runtime.repo import DEFAULT_PREFIX

    zfunc_dir = DEFAULT_PREFIX / "zfunc"
    zfunc_dir.mkdir(parents=True, exist_ok=True)
    dest = zfunc_dir / "_tessera"
    # Render typer's canonical zsh completion script (the same text typer's
    # own installer would write to ~/.zfunc/_tessera) but land it under the
    # prefix instead of $HOME.
    from typer.completion import get_completion_script

    script = get_completion_script(
        prog_name="tessera", complete_var="_TESSERA_COMPLETE", shell="zsh"
    )
    dest.write_text(script, encoding="utf-8")
    dest.chmod(0o644)
    typer.echo(f"wrote zsh completion to {dest}")
    typer.echo("add this to your ~/.zshrc (or source it):")
    typer.echo(f"  fpath+={zfunc_dir}")
    typer.echo("  autoload -Uz compinit; compinit")


# --------------------------------------------------------------------------- #
# ticket commands
# --------------------------------------------------------------------------- #
@app.command("create")
def create(
    ticket_id: str = typer.Argument(..., help="Ticket id, e.g. HQ_BR-010"),
    ticket_type: str = typer.Option("task", "--type", help="Ticket type"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Scaffold a new .ticket directory + minimal manifest."""
    from tessera_runtime.models import Owner, TicketMetadata

    root = _find_root(repo)
    ticket_dir = root / "TicketsRepository" / f"{ticket_id}.ticket"
    if ticket_dir.exists():
        typer.echo(f"error: {ticket_dir} already exists", err=True)
        raise typer.Exit(1)
    ticket_dir.mkdir(parents=True)

    metadata = TicketMetadata(
        id=ticket_id,
        title=ticket_id,
        type=ticket_type,
        owner=Owner(name="cli", type="user"),
    )
    metadata.write(ticket_dir / "metadata.json")
    (ticket_dir / "state.json").write_text(
        '{"status": "created", "updated_at": "1970-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    manifest = (
        "apiVersion: ticket/v1\n"
        "kind: Ticket\n"
        "metadata:\n"
        f"  id: {ticket_id}\n"
        f"  title: {ticket_id}\n"
        f"  type: {ticket_type}\n"
        "hooks: {}\n"
        "actions: {}\n"
    )
    (ticket_dir / "MANIFEST.yaml").write_text(manifest, encoding="utf-8")
    typer.echo(f"Created {ticket_id}.ticket")


@app.command("inspect")
def inspect(
    ticket_id: str = typer.Argument(...),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Print metadata, state, and manifest summary for a ticket."""
    from tessera_sdk import Runtime

    root = _find_root(repo)
    client = Runtime.direct(repo=root)
    try:
        ticket = client.get_ticket(ticket_id)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"id:    {ticket.id}")
    typer.echo(f"title: {ticket.metadata.title}")
    typer.echo(f"type:  {ticket.metadata.type}")
    typer.echo(f"state: {ticket.status}")
    typer.echo(f"updated_at: {ticket.state.updated_at.isoformat()}")


@app.command("transition")
def transition_cmd(
    ticket_id: str = typer.Argument(...),
    status: str = typer.Argument(..., help="Target state, e.g. running"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Request a lifecycle transition (validated per Part VI).

    Mutating command: requires a running runtime (Part X §8); when the
    runtime is not running, errors with "start runtime".
    """
    from tessera_sdk import Runtime

    root = _find_root(repo)
    if not _runtime_running(root):
        typer.echo("error: runtime not running — start it with 'ticket runtime start'", err=True)
        raise typer.Exit(1)
    client = Runtime.connect(repo=root)
    try:
        result = client.transition(ticket_id, status)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{ticket_id} -> {result['to_status']}")


@app.command("action")
def action_cmd(
    ticket_id: str = typer.Argument(...),
    action: str = typer.Argument(...),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Invoke a named manifest action.

    Mutating command: requires a running runtime (Part X §8); when the
    runtime is not running, errors with "start runtime".
    """
    from tessera_sdk import Runtime

    root = _find_root(repo)
    if not _runtime_running(root):
        typer.echo("error: runtime not running — start it with 'ticket runtime start'", err=True)
        raise typer.Exit(1)
    client = Runtime.connect(repo=root)
    try:
        result = client.invoke_action(ticket_id, action)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"action {action} on {ticket_id}: exit={result['exit_code']}"
    )
    if result.get("stdout"):
        typer.echo(result["stdout"].rstrip())
    if result.get("stderr"):
        typer.echo(result["stderr"].rstrip(), err=True)


@app.command("validate")
def validate_cmd(
    ticket_id: str = typer.Argument(...),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Validate a ticket's MANIFEST.yaml against the schema."""
    from tessera_runtime.runtime.manifest import (
        ManifestValidationError,
        load_manifest,
        validate_manifest,
    )

    root = _find_root(repo)
    manifest_path = root / "TicketsRepository" / f"{ticket_id}.ticket" / "MANIFEST.yaml"
    if not manifest_path.is_file():
        typer.echo(f"error: no MANIFEST.yaml for {ticket_id}", err=True)
        raise typer.Exit(1)
    try:
        manifest = load_manifest(manifest_path, ticket_id=ticket_id)
    except ManifestValidationError as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(1)
    warnings = validate_manifest(manifest)
    if warnings:
        typer.echo("valid with warnings:")
        for warning in warnings:
            typer.echo(f"  - {warning}")
    else:
        typer.echo("valid")


@app.command("log")
def log_cmd(
    ticket_id: str = typer.Argument(...),
    tail: int = typer.Option(20, "--tail", help="Last N lines"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Tail a ticket's activity.jsonl."""
    root = _find_root(repo)
    log_path = root / "TicketsRepository" / f"{ticket_id}.ticket" / "activity.jsonl"
    if not log_path.is_file():
        typer.echo(f"error: no activity log for {ticket_id}", err=True)
        raise typer.Exit(1)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    for line in lines[-tail:]:
        typer.echo(line)


def _runtime_running(root: Path) -> bool:
    from tessera_runtime.repo import RUNTIME_DIR_NAME, repo_init
    from tessera_runtime.runtime.server import runtime_socket_path

    try:
        return runtime_socket_path(root).exists()
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    """Console entry point (pyproject ``[project.scripts]``)."""
    app()


if __name__ == "__main__":
    main()
