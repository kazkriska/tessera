"""Python runner plugin.

Responsibility (Master Part III §4.1, RFC-0004): execute `runtime: python`
hooks/actions inside the project `uv` venv, passing the resolved environment
and event payload, and returning the runner result to the Executor.

TODO(Phase D): implement invocation contract.
"""
