#!/usr/bin/env bash
# Tessera v1 — bootstrap installer (curl target).
#
#   curl --proto '=https' --tlsv1.2 -fsSL \
#     https://github.com/kazkriska/tessera/releases/download/v1.0.0/install.sh | bash
#
# This file is intentionally tiny: it downloads the release tarball, verifies
# its SHA256, extracts it to a temp dir, and hands off to the install modules.
# All real installation logic lives in install-modules/.
set -euo pipefail

# --------------------------------------------------------------------------- #
# Release coordinates (literal, baked at release time)
# --------------------------------------------------------------------------- #
RELEASE_BASE_URL="https://github.com/kazkriska/tessera/releases/download/v1.0.0"
TARBALL_NAME="tessera-1.0.0.tar.gz"

# PLACEHOLDER — the build pipeline MUST rewrite this literal with the real
# digest of ${TARBALL_NAME} before publishing the release:
#   sha256sum dist/tessera-1.0.0.tar.gz | awk '{print $1}'
# then substitute it below. Shipping with the placeholder makes every install
# fail closed at the verification phase (by design).
EXPECTED_SHA256="f4cef7c54505ae894c54a2875103ac119553c06937e619760fee29c75b92296c"

TESSERA_VERSION="1.0.0"

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
    _C_BAN=$'\033[1;36m'; _C_OK=$'\033[1;32m'; _C_ERR=$'\033[1;31m'
    _C_WARN=$'\033[1;33m'; _C_OFF=$'\033[0m'
else
    _C_BAN=""; _C_OK=""; _C_ERR=""; _C_WARN=""; _C_OFF=""
fi

banner() {
    printf '%s\n' "${_C_BAN}==> [tessera] $*${_C_OFF}"
}
info()  { printf '%s\n' "    $*"; }
ok()    { printf '%s\n' "${_C_OK}    ok: $*${_C_OFF}"; }
warn()  { printf '%s\n' "${_C_WARN}    warn: $*${_C_OFF}" >&2; }
die()   { printf '%s\n' "${_C_ERR}    error: $*${_C_OFF}" >&2; exit 1; }

usage() {
    cat <<EOF
Tessera v${TESSERA_VERSION} installer

Usage: install.sh [OPTIONS] [-- MODULE_ARGS...]

Options:
  --check      Download and SHA256-verify the release only. No extraction,
               no installation. Exit 0 when the artifact is trustworthy.
  --dry-run    Download, verify, extract and print the install plan. Nothing
               is installed and no module is executed.
  -h, --help   Show this help.

Environment:
  TESSERA_TARBALL   Path to a local ${TARBALL_NAME} to use instead of
                    downloading (still SHA256-verified). Offline/CI use.
  TESSERA_PREFIX    Install prefix (default: \$HOME/.local/share/tessera).

All other arguments are forwarded verbatim to install-modules/main.sh.
EOF
}

# --------------------------------------------------------------------------- #
# Arg parsing (bootstrap-level flags only; the rest passes through)
# --------------------------------------------------------------------------- #
MODE="install"
PASSTHRU=()
for arg in "$@"; do
    case "$arg" in
        --check)     MODE="check" ;;
        --dry-run)   MODE="dry-run"; PASSTHRU+=("$arg") ;;
        -h|--help)   usage; exit 0 ;;
        *)           PASSTHRU+=("$arg") ;;
    esac
done

# --------------------------------------------------------------------------- #
# Temp workspace
# --------------------------------------------------------------------------- #
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tessera-install.XXXXXXXX")"
BOOTSTRAP_CLEAN=1

bootstrap_cleanup() {
    # Only ever removes our own temp workspace. Never touches an install prefix.
    if [ "${BOOTSTRAP_CLEAN}" = "1" ] && [ -n "${WORK_DIR:-}" ] && [ -d "${WORK_DIR}" ]; then
        cd /
        rm -rf -- "${WORK_DIR}"
    fi
}
trap bootstrap_cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
# Phase 1 — fetch
# --------------------------------------------------------------------------- #
banner "phase 1/4 · fetch"
TARBALL_PATH="${WORK_DIR}/${TARBALL_NAME}"

if [ -n "${TESSERA_TARBALL:-}" ]; then
    [ -f "${TESSERA_TARBALL}" ] || die "TESSERA_TARBALL not found: ${TESSERA_TARBALL}"
    info "using local tarball: ${TESSERA_TARBALL}"
    cp -- "${TESSERA_TARBALL}" "${TARBALL_PATH}"
else
    command -v curl >/dev/null 2>&1 || die "curl is required to download the release"
    URL="${RELEASE_BASE_URL}/${TARBALL_NAME}"
    info "downloading ${URL}"
    # TLS-only: refuse plaintext and any protocol downgrade on redirect.
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
         --fail --silent --show-error --location \
         --retry 3 --retry-delay 2 --connect-timeout 20 \
         --output "${TARBALL_PATH}" "${URL}" \
        || die "download failed: ${URL}"
fi
[ -s "${TARBALL_PATH}" ] || die "downloaded artifact is empty: ${TARBALL_PATH}"
ok "artifact present ($(wc -c <"${TARBALL_PATH}" | tr -d ' ') bytes)"

# --------------------------------------------------------------------------- #
# Phase 2 — verify
# --------------------------------------------------------------------------- #
banner "phase 2/4 · verify sha256"
if [ "${EXPECTED_SHA256}" = "REPLACE_WITH_REAL_SHA256" ]; then
    die "this installer was published without a real SHA256 (still the build placeholder). Refusing to install."
fi

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(sha256sum "${TARBALL_PATH}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(shasum -a 256 "${TARBALL_PATH}" | awk '{print $1}')"
else
    die "no sha256 tool found (need sha256sum or shasum). Refusing to install unverified code."
fi

if [ "${ACTUAL_SHA256}" != "${EXPECTED_SHA256}" ]; then
    printf '%s\n' "${_C_ERR}    expected: ${EXPECTED_SHA256}${_C_OFF}" >&2
    printf '%s\n' "${_C_ERR}    actual:   ${ACTUAL_SHA256}${_C_OFF}" >&2
    die "SHA256 mismatch — the artifact is corrupt or tampered with. Aborting."
fi
ok "sha256 verified: ${ACTUAL_SHA256}"

if [ "${MODE}" = "check" ]; then
    banner "check complete · artifact is trustworthy · nothing installed"
    exit 0
fi

# --------------------------------------------------------------------------- #
# Phase 3 — extract
# --------------------------------------------------------------------------- #
banner "phase 3/4 · extract"
EXTRACT_DIR="${WORK_DIR}/extract"
mkdir -p "${EXTRACT_DIR}"
command -v tar >/dev/null 2>&1 || die "tar is required to extract the release"
tar -xzf "${TARBALL_PATH}" -C "${EXTRACT_DIR}" || die "extraction failed"

# The tarball has a single top-level directory; fall back to the extract root.
SRC_DIR="${EXTRACT_DIR}"
if [ "$(find "${EXTRACT_DIR}" -mindepth 1 -maxdepth 1 -type d | wc -l)" = "1" ] \
   && [ ! -f "${EXTRACT_DIR}/install-modules/main.sh" ]; then
    SRC_DIR="$(find "${EXTRACT_DIR}" -mindepth 1 -maxdepth 1 -type d)"
fi
MAIN_MODULE="${SRC_DIR}/install-modules/main.sh"
[ -f "${MAIN_MODULE}" ] || die "install-modules/main.sh missing from the release payload"
ok "extracted to ${SRC_DIR}"

# --------------------------------------------------------------------------- #
# Phase 4 — hand off to the install modules
# --------------------------------------------------------------------------- #
export TESSERA_VERSION
export TESSERA_WORK_DIR="${WORK_DIR}"
export TESSERA_TARBALL_PATH="${TARBALL_PATH}"
export TESSERA_SRC_DIR="${SRC_DIR}"
export TESSERA_PREFIX="${TESSERA_PREFIX:-${HOME}/.local/share/tessera}"

if [ "${MODE}" = "dry-run" ]; then
    banner "phase 4/4 · dry run · install plan"
    info "version .............. ${TESSERA_VERSION}"
    info "source ............... ${SRC_DIR}"
    info "prefix ............... ${TESSERA_PREFIX}"
    info "cli symlinks ......... ${HOME}/.local/bin/tessera, ${HOME}/.local/bin/ticket"
    info "systemd user unit .... ${HOME}/.config/systemd/user/tessera-runtime@.service"
    info "modules .............. 00-precheck, 01-perms, 02-scaffold, 03-test, 04-cleanup"
    info "scope ................ user (no sudo, nothing written outside \$HOME)"
    banner "dry run complete · nothing was installed"
    exit 0
fi

banner "phase 4/4 · running install modules"
# main.sh owns cleanup of WORK_DIR from here on (its own EXIT trap); drop ours
# so the exec'd process is the single owner of the temp workspace.
BOOTSTRAP_CLEAN=0
trap - EXIT INT TERM
if [ ${#PASSTHRU[@]} -gt 0 ]; then
    exec bash "${MAIN_MODULE}" "${PASSTHRU[@]}"
else
    exec bash "${MAIN_MODULE}"
fi
