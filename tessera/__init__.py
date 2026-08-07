"""Tessera SDK — public Python client package.

The in-process/socket SDK described in Master Part X: the surface external code
uses to talk to a Tessera runtime.

Phase A scaffolding only — no behavior implemented yet.
"""

__version__ = "0.1.0"


class Runtime:
    """Handle onto a Tessera runtime (SDK facade).

    Will expose the two documented access modes (Master Part X):

    * ``connect()`` — attach to a running daemon over ``runtime.sock``.
    * ``direct()``  — drive a TicketRepository in-process, no daemon.

    TODO(Phase G): implement connect/direct and the ticket/action API.
    """


__all__ = ["Runtime", "__version__"]
