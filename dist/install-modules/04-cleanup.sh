#!/usr/bin/env bash
# 04-cleanup.sh — remove installation scratch, keep the installed app.
#
# Removes: the downloaded tarball copy, the throwaway e2e repo and its
# artifacts, and any per-run state. The extraction dir itself is unlinked by
# main.sh's EXIT trap (this script is still executing from inside it).
#
# HARD RULE: $TESSERA_PREFIX and anything under it is NEVER removed here.
set -euo pipefail

: "${TESSERA_PREFIX:=${HOME}/.local/share/tessera}"
: "${TESSERA_WORK_DIR:=}"
: "${TESSERA_TARBALL_PATH:=}"
: "${TESSERA_E2E_TMP:=}"

info() { printf '%s\n' "    $*"; }
ok()   { printf '%s\n' "    ok: $*"; }
warn() { printf '%s\n' "    warn: $*" >&2; }

PREFIX_REAL="$(cd "${TESSERA_PREFIX}" 2>/dev/null && pwd -P || printf '%s' "${TESSERA_PREFIX}")"

# Refuse to delete anything that is $HOME, /, or inside the install prefix.
safe_rm() {
    local target="$1" label="$2" real
    [ -n "${target}" ] || return 0
    [ -e "${target}" ] || return 0

    case "${target}" in
        /|"${HOME}"|"${HOME}"/) warn "refusing to remove ${target}"; return 0 ;;
    esac

    real="$(cd "$(dirname -- "${target}")" 2>/dev/null && pwd -P || true)/$(basename -- "${target}")"
    if [ -n "${PREFIX_REAL}" ] && { [ "${real}" = "${PREFIX_REAL}" ] || [ "${real#"${PREFIX_REAL}"/}" != "${real}" ]; }; then
        warn "refusing to remove ${target} (inside install prefix)"
        return 0
    fi

    rm -rf -- "${target}" 2>/dev/null \
        && ok "removed ${label}: ${target}" \
        || warn "could not remove ${label}: ${target}"
}

# 1. downloaded tarball copy
safe_rm "${TESSERA_TARBALL_PATH}" "tarball"

# 2. throwaway e2e repo + artifacts from 03-test.sh
safe_rm "${TESSERA_E2E_TMP}" "e2e temp repo"
if [ -n "${TESSERA_WORK_DIR}" ]; then
    safe_rm "${TESSERA_WORK_DIR}/e2e-artifacts" "e2e artifacts"
fi

# 3. leftover scratch from earlier runs of this installer
for stale in "${TMPDIR:-/tmp}"/tessera-e2e.*; do
    [ -e "${stale}" ] || continue
    safe_rm "${stale}" "stale e2e scratch"
done

# 4. the extraction dir: deferred to main.sh's EXIT trap (we run from inside it)
if [ -n "${TESSERA_WORK_DIR}" ] && [ -d "${TESSERA_WORK_DIR}" ]; then
    info "extraction dir ${TESSERA_WORK_DIR} will be removed on exit"
fi

info "kept: ${TESSERA_PREFIX} (installed application)"
ok "cleanup complete"
