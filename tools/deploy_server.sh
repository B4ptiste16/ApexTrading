#!/usr/bin/env bash
# APEX · Oracle server deploy
# ─────────────────────────────────────────────────────────────────
# Mirrors the local server/ tree to /opt/server on Oracle and
# restarts apex_server.service. Idempotent — re-running is safe.
#
# Usage:
#   ./tools/deploy_server.sh                  # full deploy
#   ./tools/deploy_server.sh --restart-only   # skip rsync, just restart
#   ./tools/deploy_server.sh --pip "<pkg>"    # install one pkg in venv
#
# Auth: assumes ssh key at "$HOME/Documents/oracle server/ssh-key-2026-05-20.key"
# (override via APEX_ORACLE_KEY env var). Tested against the Oracle
# Linux box at 145.241.170.165 with user `opc`.

set -euo pipefail

ORACLE_HOST="${APEX_ORACLE_HOST:-145.241.170.165}"
ORACLE_USER="${APEX_ORACLE_USER:-opc}"
ORACLE_KEY="${APEX_ORACLE_KEY:-$HOME/Documents/oracle server/ssh-key-2026-05-20.key}"
ORACLE_SERVER_DIR="${APEX_ORACLE_SERVER_DIR:-/opt/server}"
ORACLE_VENV_PYTHON="${APEX_ORACLE_VENV_PYTHON:-/opt/apex_venv/bin/python}"
ORACLE_SERVICE="${APEX_ORACLE_SERVICE:-apex_server.service}"

SSH="ssh -i \"$ORACLE_KEY\" -o StrictHostKeyChecking=no $ORACLE_USER@$ORACLE_HOST"
SCP="scp -i \"$ORACLE_KEY\" -o StrictHostKeyChecking=no"

run_ssh() { eval "$SSH \"$1\""; }

deploy_files() {
    echo "→ rsync server/*.py to /tmp on Oracle"
    # rsync isn't always installed; fall back to scp of the files we own.
    # Excludes __pycache__ + .pyc to keep the upload tiny.
    local files
    files=$(ls server/*.py)
    eval "$SCP $files $ORACLE_USER@$ORACLE_HOST:/tmp/"

    echo "→ move into $ORACLE_SERVER_DIR with sudo"
    local server_files
    server_files=""
    for f in server/*.py; do
        server_files="$server_files /tmp/$(basename "$f")"
    done
    run_ssh "sudo cp $server_files $ORACLE_SERVER_DIR/ && rm $server_files"
}

restart_service() {
    echo "→ restart $ORACLE_SERVICE"
    run_ssh "sudo systemctl restart $ORACLE_SERVICE"
    sleep 2
    run_ssh "sudo systemctl status $ORACLE_SERVICE --no-pager -l | head -12"
}

pip_install() {
    local pkg="$1"
    echo "→ pip install $pkg into shared venv"
    run_ssh "sudo $ORACLE_VENV_PYTHON -m pip install --quiet $pkg && echo \"  installed: $pkg\""
}

case "${1:-deploy}" in
    --restart-only)
        restart_service
        ;;
    --pip)
        if [ -z "${2:-}" ]; then
            echo "usage: $0 --pip <pip-package-name>" >&2
            exit 2
        fi
        pip_install "$2"
        ;;
    deploy|"")
        if [ ! -f "$ORACLE_KEY" ]; then
            echo "ERROR: SSH key not found at: $ORACLE_KEY" >&2
            echo "Set APEX_ORACLE_KEY env var or fix the default path." >&2
            exit 1
        fi
        deploy_files
        restart_service
        echo ""
        echo "✓ Oracle deploy complete."
        ;;
    *)
        echo "usage: $0 [deploy|--restart-only|--pip <pkg>]" >&2
        exit 2
        ;;
esac
