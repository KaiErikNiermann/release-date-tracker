#!/usr/bin/env bash
# Install rdt (release-date-tracker) for the current user.
#
# Runs from a checkout or straight from the network:
#   ./install.sh
#   curl -fsSL https://raw.githubusercontent.com/KaiErikNiermann/release-date-tracker/main/install.sh | bash
#
# Prefers uv, falls back to pipx, then to `python -m venv` + a shim — in every case the
# tool lands in an isolated environment, never in the system site-packages.
set -euo pipefail

REPO="KaiErikNiermann/release-date-tracker"
SPEC="${RDT_INSTALL_SPEC:-git+https://github.com/${REPO}.git@${RDT_VERSION:-main}}"
BIN_DIR="${RDT_BIN_DIR:-$HOME/.local/bin}"
MIN_PY="3.13"

info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# The three tiers below all need a new enough interpreter; check once, up front, so the
# failure is a sentence rather than a stack trace from inside a build backend.
find_python() {
    for candidate in python3.14 python3.13 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

main() {
    local py
    py="$(find_python)" || die "rdt needs Python >= ${MIN_PY}; none found on PATH."
    info "using $($py --version)"

    if command -v uv >/dev/null 2>&1; then
        info "installing with uv"
        uv tool install --force --python "$py" "$SPEC"
    elif command -v pipx >/dev/null 2>&1; then
        info "installing with pipx"
        pipx install --force --python "$py" "$SPEC"
    else
        # No tool manager: build a venv we own and drop a shim on PATH. Equivalent
        # isolation, just without the upgrade/uninstall bookkeeping the others give.
        local venv="${RDT_VENV:-$HOME/.local/share/rdt/venv}"
        info "no uv or pipx found — installing into $venv"
        "$py" -m venv "$venv"
        "$venv/bin/python" -m pip install --quiet --upgrade pip
        "$venv/bin/python" -m pip install --quiet "$SPEC"
        mkdir -p "$BIN_DIR"
        ln -sf "$venv/bin/rdt" "$BIN_DIR/rdt"
        info "linked $BIN_DIR/rdt"
    fi

    command -v rdt >/dev/null 2>&1 || warn "$BIN_DIR is not on your PATH — add it to your shell profile."

    cat <<'NEXT'

Installed. Next:

  rdt --help                 # what it can do
  rdt tui                    # the interactive browser

Optional API keys (everything keyless still works without them) go in
~/.config/rdt/.env — see .env.example in the repo for the full list:

  TMDB_API_KEY=...           # movies / TV
  TWITCH_CLIENT_ID=...       # games, via IGDB
  TWITCH_CLIENT_SECRET=...
  OPENAI_API_KEY=...         # the Tier-1 gap-filler

Your tracker lives under ~/.local/share/rdt (XDG); override with RDT_DB_PATH.
NEXT
}

main "$@"
