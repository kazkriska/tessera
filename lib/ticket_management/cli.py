"""Tessera CLI — Typer application (Part X, RFC-0010).

The CLI wraps the :class:`tessera.Runtime` client; there is no duplicated
logic (Part XI §4.4). Commands that need a live runtime (``action``,
``transition``) operate directly on the filesystem when no runtime is
running (Part X §8).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from lib.ticket_management.config import RuntimeConfig, load_config

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
    from tessera import find_repo_root

    try:
        return find_repo_root(path)
    except RuntimeError:
        if path:
            return Path(path).resolve()
        return Path.cwd().resolve()


def _client(path: str | None, attach: bool = False):
    from tessera import Runtime

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
    import subprocess
    import sys

    from lib.ticket_management.repo import RUNTIME_DIR_NAME, repo_init
    from lib.ticket_management.runtime.server import _socket_is_live, runtime_socket_path

    root = _find_root(repo)
    sock_path = runtime_socket_path(root)
    if sock_path.exists():
        # Live or stale — let the server decide, but for UX, a live one is an
        # error; a stale one will be reaped by the daemon we're about to spawn.
        if _socket_is_live(sock_path):
            typer.echo(f"error: runtime already running at {sock_path}", err=True)
            raise typer.Exit(1)

    repo_init(root)
    log_dir = root / "TicketsRepository" / RUNTIME_DIR_NAME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "runtime.log"

    # Detached daemon: a fresh interpreter runs the server and blocks in
    # wait() until `runtime stop` sends the shutdown RPC.
    daemon_code = (
        "import sys, signal\n"
        "from lib.ticket_management.runtime.server import RuntimeServer\n"
        f"root = {str(root)!r}\n"
        # No config passed: the Pipeline loads `.ticket-runtime/config.yaml`
        # itself at boot (RFC-0004 boot step 1), so user-set values apply.
        "srv = RuntimeServer(root)\n"
        "def _term(*_):\n"
        "    srv.stop()\n"
        "signal.signal(signal.SIGTERM, _term)\n"
        "signal.signal(signal.SIGINT, _term)\n"
        "srv.start()\n"
        "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
        "srv.wait()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", daemon_code],
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait until the socket is live (or the daemon died).
    import time

    deadline = time.time() + 15
    while time.time() < deadline:
        if _socket_is_live(sock_path):
            typer.echo(f"Runtime started (sock {sock_path}, pid {proc.pid})")
            return
        if proc.poll() is not None:
            typer.echo(
                f"error: runtime daemon exited with code {proc.returncode}; "
                f"see {log_file}",
                err=True,
            )
            raise typer.Exit(1)
        time.sleep(0.1)
    typer.echo("error: runtime did not start within 15s", err=True)
    raise typer.Exit(1)


@runtime_app.command("stop")
def runtime_stop(
    repo: Optional[str] = typer.Option(None, "--repo", help="Framework root"),
) -> None:
    """Graceful shutdown of the runtime daemon."""
    root = _find_root(repo)
    from tessera import Runtime, RuntimeNotRunning

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
    from lib.ticket_management.repo import RUNTIME_DIR_NAME, repo_init
    from lib.ticket_management.runtime.server import runtime_socket_path

    sock = runtime_socket_path(root)
    running = sock.exists()
    typer.echo(f"root:    {root}")
    typer.echo(f"socket:  {sock}")
    typer.echo(f"running: {'yes' if running else 'no'}")
    if not running:
        return
    from tessera import Runtime

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
    """Scaffold TicketRepository/ + .ticket-runtime/."""
    from lib.ticket_management.repo import repo_init

    root = Path(path).resolve() if path else Path.cwd().resolve()
    repo = repo_init(root)
    typer.echo(f"Initialized Tessera repository at {repo}")


@repo_app.command("scan")
def repo_scan(
    path: Optional[str] = typer.Argument(None, help="Framework root"),
) -> None:
    """Force rediscovery + registry rebuild."""
    from lib.ticket_management.repo import rescan

    root = _find_root(path)
    result = rescan(root)
    count = len(result["registry"].list_all()) if result.get("registry") else 0
    typer.echo(f"Scanned {root}; {count} tickets in registry.")


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
    from lib.ticket_management.models import Owner, TicketMetadata

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
    from tessera import Runtime

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
    from tessera import Runtime

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
    from tessera import Runtime

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
    from lib.ticket_management.runtime.manifest import (
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
    from lib.ticket_management.repo import RUNTIME_DIR_NAME, repo_init
    from lib.ticket_management.runtime.server import runtime_socket_path

    try:
        return runtime_socket_path(root).exists()
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    """Console entry point (pyproject ``[project.scripts]``)."""
    app()


if __name__ == "__main__":
    main()
