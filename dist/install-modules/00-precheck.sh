#!/usr/bin/env bash
# 00-precheck.sh — environment preflight.
#
# Verifies the host can run Tessera v1 (charter §7: Linux-only), ensures a
# Python >= 3.12 toolchain exists (installing uv + a managed 3.12 when it does
# not), and confirms the user-scope install directories are writable.
set -euo pipefail

: "${TESSERA_STATE_FILE:=${TMPDIR:-/tmp}/tessera-install-state.$$.env}"
: "${TESSERA_BIN_DIR:=${HOME}/.local/bin}"

info() { printf '%s\n' "    $*"; }
ok()   { printf '%s\n' "    ok: $*"; }
warn() { printf '%s\n' "    warn: $*" >&2; }
die()  { printf '%s\n' "    error: $*" >&2; exit 1; }

state_set() { printf '%s=%s\n' "$1" "$2" >>"${TESSERA_STATE_FILE}"; }

# --------------------------------------------------------------------------- #
# 1. OS gate — Linux only (charter §7)
# --------------------------------------------------------------------------- #
OS_NAME="$(uname -s)"
if [ "${OS_NAME}" != "Linux" ]; then
    die "Tessera v1 supports Linux only (charter §7); detected '${OS_NAME}'. Aborting."
fi
ok "os: Linux ($(uname -r))"

# --------------------------------------------------------------------------- #
# 2. Required base tools
# --------------------------------------------------------------------------- #
for tool in curl tar; do
    command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done
ok "base tools: curl, tar"

# --------------------------------------------------------------------------- #
# 3. Write access to the user-scope directories
# --------------------------------------------------------------------------- #
for d in "${HOME}/.local/share" "${TESSERA_BIN_DIR}"; do
    mkdir -p "${d}" 2>/dev/null || die "cannot create ${d} (user-scope install needs a writable \$HOME)"
    [ -w "${d}" ] || die "no write access to ${d}"
done
ok "writable: ${HOME}/.local/share, ${TESSERA_BIN_DIR}"

# --------------------------------------------------------------------------- #
# 4. Python >= 3.12
# --------------------------------------------------------------------------- #
PATH="${TESSERA_BIN_DIR}:${HOME}/.cargo/bin:${PATH}"
export PATH

python_ok() {
    [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
        >/dev/null 2>&1
}

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 && python_ok "${candidate}"; then
        PYTHON_BIN="$(command -v "${candidate}")"
        break
    fi
done

# --------------------------------------------------------------------------- #
# 5. uv — required for the install itself (uv pip install)
# --------------------------------------------------------------------------- #
install_uv() {
    info "installing uv (astral.sh)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
        || die "uv installation failed (https://astral.sh/uv/install.sh)"
    PATH="${TESSERA_BIN_DIR}:${HOME}/.cargo/bin:${PATH}"
    export PATH
    hash -r 2>/dev/null || true
}

UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "${UV_BIN}" ]; then
    install_uv
    UV_BIN="$(command -v uv 2>/dev/null || true)"
    [ -n "${UV_BIN}" ] || die "uv not on PATH after install; add ${TESSERA_BIN_DIR} to PATH and retry"
    ok "uv installed: ${UV_BIN} ($("${UV_BIN}" --version 2>/dev/null || echo unknown))"
else
    ok "uv present: ${UV_BIN} ($("${UV_BIN}" --version 2>/dev/null || echo unknown))"
fi

if [ -z "${PYTHON_BIN}" ]; then
    info "no python >= 3.12 found; provisioning one via uv…"
    "${UV_BIN}" python install 3.12 >/dev/null 2>&1 \
        || die "uv python install 3.12 failed"
    hash -r 2>/dev/null || true
    for candidate in python3.12 python3; do
        if command -v "${candidate}" >/dev/null 2>&1 && python_ok "${candidate}"; then
            PYTHON_BIN="$(command -v "${candidate}")"
            break
        fi
    done
    if [ -z "${PYTHON_BIN}" ]; then
        # uv-managed interpreters are not always on PATH; ask uv directly.
        PYTHON_BIN="$("${UV_BIN}" python find 3.12 2>/dev/null || true)"
    fi
    [ -n "${PYTHON_BIN}" ] || die "python >= 3.12 still unavailable after 'uv python install 3.12'"
    ok "python provisioned: ${PYTHON_BIN}"
else
    ok "python: ${PYTHON_BIN} ($("${PYTHON_BIN}" -V 2>&1))"
fi

# --------------------------------------------------------------------------- #
# 6. Publish findings for the later modules
# --------------------------------------------------------------------------- #
state_set TESSERA_PYTHON_BIN "${PYTHON_BIN}"
state_set TESSERA_UV_BIN "${UV_BIN}"
state_set PATH "${PATH}"

case ":${PATH}:" in
    *":${TESSERA_BIN_DIR}:"*) ;;
    *) warn "${TESSERA_BIN_DIR} is not on your PATH — add it to your shell profile to use 'tessera'/'ticket'" ;;
esac

ok "precheck passed"
