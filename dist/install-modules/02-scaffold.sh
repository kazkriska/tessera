#!/usr/bin/env bash
# 02-scaffold.sh — install the runtime + SDK, register CLI and systemd unit.
#
#   $PREFIX/venv                       uv-managed virtualenv (python >= 3.12)
#   $PREFIX/venv/bin/{tessera,ticket}  console scripts from pyproject
#   ~/.local/bin/{tessera,ticket}      symlinks onto the above
#   ~/.config/systemd/user/tessera-runtime@.service   template unit
set -euo pipefail

: "${TESSERA_STATE_FILE:=${TMPDIR:-/tmp}/tessera-install-state.$$.env}"
: "${TESSERA_PREFIX:=${HOME}/.local/share/tessera}"
: "${TESSERA_BIN_DIR:=${HOME}/.local/bin}"
: "${TESSERA_SYSTEMD_USER_DIR:=${HOME}/.config/systemd/user}"
: "${TESSERA_VERSION:=1.0.0}"
: "${TESSERA_UV_BIN:=uv}"
: "${TESSERA_PYTHON_BIN:=python3}"
if [ -z "${TESSERA_SRC_DIR:-}" ]; then
    TESSERA_SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fi

info() { printf '%s\n' "    $*"; }
ok()   { printf '%s\n' "    ok: $*"; }
warn() { printf '%s\n' "    warn: $*" >&2; }
die()  { printf '%s\n' "    error: $*" >&2; exit 1; }

state_set() { printf '%s=%s\n' "$1" "$2" >>"${TESSERA_STATE_FILE}"; }

[ -f "${TESSERA_SRC_DIR}/pyproject.toml" ] \
    || die "no pyproject.toml in ${TESSERA_SRC_DIR} — payload is not a Tessera source tree"

# --------------------------------------------------------------------------- #
# 1. Virtualenv under the prefix
# --------------------------------------------------------------------------- #
VENV_DIR="${TESSERA_PREFIX}/venv"
mkdir -p "${TESSERA_PREFIX}"
info "creating virtualenv at ${VENV_DIR}"

# --clear makes re-installs / upgrades over an existing prefix idempotent
# (plain `uv venv` refuses to reuse a populated directory). The bare form is a
# fallback for uv builds that predate --clear.
create_venv() {
    local out
    if out="$("${TESSERA_UV_BIN}" venv --python 3.12 --clear "${VENV_DIR}" 2>&1)"; then return 0; fi
    if out="$("${TESSERA_UV_BIN}" venv --clear "${VENV_DIR}" 2>&1)"; then return 0; fi
    if out="$("${TESSERA_UV_BIN}" venv "${VENV_DIR}" 2>&1)"; then return 0; fi
    printf '%s\n' "${out}" >&2
    return 1
}
create_venv || die "uv venv failed at ${VENV_DIR}"
[ -x "${VENV_DIR}/bin/python" ] || die "virtualenv is missing bin/python"
ok "virtualenv: $("${VENV_DIR}/bin/python" -V 2>&1)"

# --------------------------------------------------------------------------- #
# 2. Install the distribution (tessera_runtime + tessera_sdk + console scripts)
# --------------------------------------------------------------------------- #
info "installing tessera v${TESSERA_VERSION} from ${TESSERA_SRC_DIR}"
(
    cd "${TESSERA_SRC_DIR}"
    VIRTUAL_ENV="${VENV_DIR}" "${TESSERA_UV_BIN}" pip install --python "${VENV_DIR}/bin/python" .
) || die "uv pip install . failed"

TESSERA_BIN="${VENV_DIR}/bin/tessera"
[ -x "${TESSERA_BIN}" ] || die "console script 'tessera' not found at ${TESSERA_BIN} after install"
"${VENV_DIR}/bin/python" -c 'import tessera_runtime, tessera_sdk' \
    || die "tessera_runtime / tessera_sdk are not importable after install"
ok "packages installed (tessera_runtime, tessera_sdk)"

# --------------------------------------------------------------------------- #
# 3. CLI symlinks (idempotent)
# --------------------------------------------------------------------------- #
mkdir -p "${TESSERA_BIN_DIR}"
for cmd in tessera ticket; do
    link="${TESSERA_BIN_DIR}/${cmd}"
    if [ -e "${link}" ] && [ ! -L "${link}" ]; then
        die "${link} exists and is not a symlink — remove it and re-run"
    fi
    ln -sfn "${TESSERA_BIN}" "${link}"
    ok "symlink: ${link} -> ${TESSERA_BIN}"
done

# --------------------------------------------------------------------------- #
# 4. systemd user template unit
#
# One instance per ticket repository:
#   systemctl --user start tessera-runtime@$(systemd-escape --path /path/to/repo)
# %i is the escaped instance name, %I the unescaped path — a repo path contains
# '/', so ExecStart must use %I.
#
# `tessera runtime start` forks a detached daemon and returns, hence
# Type=oneshot + RemainAfterExit=yes with an explicit ExecStop.
# --------------------------------------------------------------------------- #
mkdir -p "${TESSERA_SYSTEMD_USER_DIR}"
UNIT_PATH="${TESSERA_SYSTEMD_USER_DIR}/tessera-runtime@.service"
cat >"${UNIT_PATH}" <<UNIT
[Unit]
Description=Tessera runtime daemon for %I
Documentation=https://github.com/kazkriska/tessera
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%I
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=TESSERA_REPO=%I
ExecStart=${TESSERA_BIN} runtime start --repo %I
ExecStop=${TESSERA_BIN} runtime stop --repo %I
TimeoutStartSec=60

[Install]
WantedBy=default.target
UNIT
chmod 0644 "${UNIT_PATH}"
ok "systemd user unit: ${UNIT_PATH}"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 \
        || warn "systemctl --user daemon-reload failed (no user session bus?) — run it manually later"
else
    warn "systemctl not found; unit written but not reloaded"
fi

# --------------------------------------------------------------------------- #
# 5. Record the install for later modules
# --------------------------------------------------------------------------- #
state_set TESSERA_VENV_DIR "${VENV_DIR}"
state_set TESSERA_BIN "${TESSERA_BIN}"
state_set TESSERA_UNIT_PATH "${UNIT_PATH}"
printf '%s\n' "${TESSERA_VERSION}" >"${TESSERA_PREFIX}/VERSION"

ok "scaffold complete"
