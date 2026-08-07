"""Env — environment resolution, merging and secret masking.

Responsibility (Master Part IX R.A.9; CONTRACTS §7): merge environment in the
order System base -> Workspace `.env` -> Ticket `.env` -> Manifest `env` ->
Event payload, and apply the secret DENYLIST (AWS_SECRET_ACCESS_KEY,
DATABASE_URL, SSH_AUTH_SOCK, SUDO_USER, ...) before handing the environment to
a subprocess.

TODO(Phase D): implement merge order and denylist masking.
"""
