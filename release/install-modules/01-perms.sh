#!/usr/bin/env bash
# 01-perms.sh — install scope + permissions.
#
# v1.0.0 is user-scope only: everything lands under $HOME, nothing needs sudo
# and nothing is written outside the user's own directories. This module is a
# deliberate stub that pins that contract and rejects (softly) --system.
set -euo pipefail

: "${TESSERA_STATE_FILE:=${TMPDIR:-/tmp}/tessera-install-state.$$.env}"
: "${TESSERA_PREFIX:=${HOME}/.local/share/tessera}"
: "${TESSERA_BIN_DIR:=${HOME}/.local/bin}"
: "${TESSERA_SYSTEMD_USER_DIR:=${HOME}/.config/systemd/user}"

info() { printf '%s\n' "    $*"; }
ok()   { printf '%s\n' "    ok: $*"; }
warn() { printf '%s\n' "    warn: $*" >&2; }
die()  { printf '%s\n' "    error: $*" >&2; exit 1; }

state_set() { printf '%s=%s\n' "$1" "$2" >>"${TESSERA_STATE_FILE}"; }

# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #
SCOPE="user"
for arg in "$@"; do
    case "${arg}" in
        --system)
            warn "system scope not supported in v1.0.0, using default user scope"
            ;;
        --user)
            ;;
    esac
done

# No sudo, ever. Refuse to keep going as root: a root install would create
# root-owned files in a user's ~/.local and break every later invocation.
if [ "$(id -u)" = "0" ]; then
    die "refusing to install as root — Tessera v1.0.0 is a user-scope install (run as your normal user)"
fi
ok "scope: ${SCOPE} (no sudo, no privileged writes)"

# --------------------------------------------------------------------------- #
# Directory layout (idempotent, 0755 / 0700 for config)
# --------------------------------------------------------------------------- #
mkdir -p "${TESSERA_PREFIX}" "${TESSERA_BIN_DIR}" "${TESSERA_SYSTEMD_USER_DIR}"
chmod 0755 "${TESSERA_PREFIX}" "${TESSERA_BIN_DIR}" 2>/dev/null || true

for d in "${TESSERA_PREFIX}" "${TESSERA_BIN_DIR}" "${TESSERA_SYSTEMD_USER_DIR}"; do
    [ -w "${d}" ] || die "no write access to ${d}"
done

# An existing prefix owned by another user (e.g. an old sudo install) would
# fail confusingly later; catch it now.
if [ -e "${TESSERA_PREFIX}" ] && [ ! -O "${TESSERA_PREFIX}" ]; then
    die "${TESSERA_PREFIX} exists but is not owned by $(id -un) — remove it and re-run"
fi

info "prefix ......... ${TESSERA_PREFIX}"
info "bin ............ ${TESSERA_BIN_DIR}"
info "systemd user ... ${TESSERA_SYSTEMD_USER_DIR}"

state_set TESSERA_SCOPE "${SCOPE}"
ok "permissions ready"
