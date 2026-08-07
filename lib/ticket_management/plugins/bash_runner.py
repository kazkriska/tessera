"""Bash runner plugin.

Responsibility (Master Part III §4.1, RFC-0004): execute `runtime: bash`
hooks/actions via a POSIX shell under the Executor's process-group isolation
and path jail.

TODO(Phase D): implement invocation contract.
"""
