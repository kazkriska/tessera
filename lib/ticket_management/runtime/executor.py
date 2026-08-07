"""Executor — subprocess isolation and runner invocation.

Responsibility (Master Part III R.A.3, Part IX §4.1; CONTRACTS §7): run each
hook/action in a new POSIX process group (`os.setsid`) with CWD pinned to the
Ticket root, enforce timeouts (SIGTERM then SIGKILL after 3s), apply the path
jail and dispatch to the appropriate language runner plugin.

TODO(Phase D): implement process spawning, timeout handling and path jail.
"""
