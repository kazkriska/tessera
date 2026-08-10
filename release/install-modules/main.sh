#!/usr/bin/env bash
# Tessera v1 — install orchestrator.
#
# Invoked by dist/install.sh (exec) once the release tarball has been
# downloaded, SHA256-verified and extracted. Can also be run directly against
# an unpacked source tree:
#
#   bash install-modules/main.sh [--system] [--dry-run]
set -euo pipefail

MODULE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${TESSERA_SRC_DIR:=$(cd -- "${MODULE_DIR}/.." && pwd)}"
: "${TESSERA_VERSION:=1.0.0}"
: "${TESSERA_PREFIX:=${HOME}/.local/share/tessera}"
: "${TESSERA_BIN_DIR:=${HOME}/.local/bin}"
: "${TESSERA_SYSTEMD_USER_DIR:=${HOME}/.config/systemd/user}"
: "${TESSERA_WORK_DIR:=}"
: "${TESSERA_TARBALL_PATH:=}"

# Shared key=value state between modules (each module is its own process).
TESSERA_STATE_FILE="${TESSERA_STATE_FILE:-${TMPDIR:-/tmp}/tessera-install-state.$$.env}"
: >"${TESSERA_STATE_FILE}"

export MODULE_DIR TESSERA_SRC_DIR TESSERA_VERSION TESSERA_PREFIX \
       TESSERA_BIN_DIR TESSERA_SYSTEMD_USER_DIR TESSERA_WORK_DIR \
       TESSERA_TARBALL_PATH TESSERA_STATE_FILE

if [ -t 1 ]; then
    _C_BAN=$'\033[1;36m'; _C_OK=$'\033[1;32m'; _C_ERR=$'\033[1;31m'; _C_OFF=$'\033[0m'
else
    _C_BAN=""; _C_OK=""; _C_ERR=""; _C_OFF=""
fi
banner() { printf '%s\n' "${_C_BAN}==> [tessera] $*${_C_OFF}"; }
ok()     { printf '%s\n' "${_C_OK}    ok: $*${_C_OFF}"; }
fail()   { printf '%s\n' "${_C_ERR}    error: $*${_C_OFF}" >&2; }

# --------------------------------------------------------------------------- #
# Cleanup — temp material ONLY. The installed application at $TESSERA_PREFIX
# is never touched here, on success or on failure.
# --------------------------------------------------------------------------- #
cleanup() {
    local rc=$?
    cd /
    # Pick up scratch paths recorded by a module that aborted before main.sh
    # could re-import the state file (e.g. 03-test.sh's throwaway repo).
    if [ -s "${TESSERA_STATE_FILE}" ]; then
        set -a
        # shellcheck disable=SC1090
        . "${TESSERA_STATE_FILE}" 2>/dev/null || true
        set +a
    fi
    if [ -n "${TESSERA_E2E_TMP:-}" ] && [ -d "${TESSERA_E2E_TMP}" ] \
       && [ "${TESSERA_E2E_TMP}" != "${HOME}" ] \
       && [ "${TESSERA_E2E_TMP#"${TESSERA_PREFIX}"}" = "${TESSERA_E2E_TMP}" ]; then
        rm -rf -- "${TESSERA_E2E_TMP}" 2>/dev/null || true
    fi
    if [ -n "${TESSERA_TARBALL_PATH}" ] && [ -f "${TESSERA_TARBALL_PATH}" ]; then
        rm -f -- "${TESSERA_TARBALL_PATH}" 2>/dev/null || true
    fi
    if [ -n "${TESSERA_WORK_DIR}" ] && [ -d "${TESSERA_WORK_DIR}" ] \
       && [ "${TESSERA_WORK_DIR}" != "/" ] && [ "${TESSERA_WORK_DIR}" != "${HOME}" ] \
       && [ "${TESSERA_WORK_DIR#"${TESSERA_PREFIX}"}" = "${TESSERA_WORK_DIR}" ]; then
        # Safe on Linux: bash keeps an open fd on the running script, so
        # unlinking the extraction dir mid-run does not truncate execution.
        rm -rf -- "${TESSERA_WORK_DIR}" 2>/dev/null || true
    fi
    rm -f -- "${TESSERA_STATE_FILE}" 2>/dev/null || true
    if [ "${rc}" -ne 0 ]; then
        fail "installation aborted (exit ${rc}); temp files removed, ${TESSERA_PREFIX} left untouched"
    fi
    return "${rc}"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
# Module sequence
# --------------------------------------------------------------------------- #
MODULES=(
    "00-precheck.sh"
    "01-perms.sh"
    "02-scaffold.sh"
    "03-test.sh"
    "04-cleanup.sh"
)

banner "tessera v${TESSERA_VERSION} · user-scope install"
printf '%s\n' "    source: ${TESSERA_SRC_DIR}"
printf '%s\n' "    prefix: ${TESSERA_PREFIX}"

step=0
total=${#MODULES[@]}
for module in "${MODULES[@]}"; do
    step=$((step + 1))
    path="${MODULE_DIR}/${module}"
    [ -f "${path}" ] || { fail "missing install module: ${module}"; exit 1; }
    banner "module ${step}/${total} · ${module}"
    if ! bash "${path}" "$@"; then
        fail "module ${module} failed"
        exit 1
    fi
    # Modules export discoveries (python/uv paths, temp dirs) via the state
    # file; re-import them so later modules see them.
    if [ -s "${TESSERA_STATE_FILE}" ]; then
        set -a
        # shellcheck disable=SC1090
        . "${TESSERA_STATE_FILE}"
        set +a
    fi
done

banner "install complete"
ok "tessera v${TESSERA_VERSION} installed at ${TESSERA_PREFIX}"
ok "cli: ${TESSERA_BIN_DIR}/tessera, ${TESSERA_BIN_DIR}/ticket"
ok "systemd user unit: generated on first 'tessera repo init' (not written now)"
printf '%s\n' "    next: tessera repo init /path/to/repo && tessera --help"
exit 0
