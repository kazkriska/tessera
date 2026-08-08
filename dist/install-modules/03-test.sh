#!/usr/bin/env bash
# 03-test.sh — post-install end-to-end verification.
#
# Runs the packaged e2e smoke test against the freshly installed binaries in a
# throwaway repository. A non-zero exit fails the install: main.sh aborts and
# its EXIT trap removes every temp artifact.
set -euo pipefail

: "${TESSERA_STATE_FILE:=${TMPDIR:-/tmp}/tessera-install-state.$$.env}"
: "${TESSERA_PREFIX:=${HOME}/.local/share/tessera}"
: "${TESSERA_VENV_DIR:=${TESSERA_PREFIX}/venv}"
: "${TESSERA_BIN:=${TESSERA_VENV_DIR}/bin/tessera}"
: "${TESSERA_BIN_DIR:=${HOME}/.local/bin}"
if [ -z "${TESSERA_SRC_DIR:-}" ]; then
    TESSERA_SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fi

info() { printf '%s\n' "    $*"; }
ok()   { printf '%s\n' "    ok: $*"; }
warn() { printf '%s\n' "    warn: $*" >&2; }
fail() { printf '%s\n' "    FAIL: $*" >&2; }

state_set() { printf '%s=%s\n' "$1" "$2" >>"${TESSERA_STATE_FILE}"; }

if [ "${TESSERA_SKIP_E2E:-0}" = "1" ]; then
    warn "TESSERA_SKIP_E2E=1 — skipping post-install verification"
    exit 0
fi

# --------------------------------------------------------------------------- #
# 1. Smoke the installed CLI itself
# --------------------------------------------------------------------------- #
[ -x "${TESSERA_BIN}" ] || { fail "installed binary not executable: ${TESSERA_BIN}"; exit 1; }
if ! "${TESSERA_BIN}" --help >/dev/null 2>&1; then
    fail "'tessera --help' failed with the installed binary"
    exit 1
fi
ok "cli responds: ${TESSERA_BIN} --help"

# --------------------------------------------------------------------------- #
# 2. Packaged e2e smoke test (dist/e2e/smoke_test.py in the release payload)
# --------------------------------------------------------------------------- #
SMOKE=""
for candidate in \
    "${TESSERA_SRC_DIR}/e2e/smoke_test.py" \
    "${TESSERA_SRC_DIR}/dist/e2e/smoke_test.py"
do
    if [ -f "${candidate}" ]; then SMOKE="${candidate}"; break; fi
done

if [ -z "${SMOKE}" ]; then
    fail "e2e/smoke_test.py is missing from the release payload — cannot verify the install"
    exit 1
fi

E2E_TMP="$(mktemp -d "${TMPDIR:-/tmp}/tessera-e2e.XXXXXXXX")"
state_set TESSERA_E2E_TMP "${E2E_TMP}"
info "running ${SMOKE}"
info "throwaway repo: ${E2E_TMP}"

set +e
PATH="${TESSERA_BIN_DIR}:${TESSERA_VENV_DIR}/bin:${PATH}" \
TESSERA_E2E_REPO="${E2E_TMP}" \
TESSERA_BIN="${TESSERA_BIN}" \
    "${TESSERA_VENV_DIR}/bin/python" "${SMOKE}" --repo "${E2E_TMP}"
rc=$?
set -e

if [ "${rc}" -ne 0 ]; then
    fail "post-install e2e smoke test failed (exit ${rc})"
    fail "the install is NOT verified; aborting and cleaning up temp artifacts"
    exit 1
fi

ok "post-install e2e passed"
