"""Node runner plugin.

Responsibility (Master Part III §4.1, RFC-0004): execute `runtime: node`
hooks/actions via the system Node interpreter under the Executor's isolation
rules.

TODO(Phase D): implement invocation contract.
"""


def node_runner(*args: object, **kwargs: object) -> None:
    """Placeholder runner callable for v1 dispatcher registration."""
    raise NotImplementedError("node_runner is not implemented in v1")
