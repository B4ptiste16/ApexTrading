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
    echo "→ scp server/*.py to /tmp on Oracle"
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

    # V4.6.21 — also sync core/*.py to /opt/apex_bots/core/. The
    # built-in trading bots (longbot_v2, shortbot_v2, daybot) and the
    # transition cleanup module + bot framework all import from there.
    # Previously only server/ was synced; core/ai_client missing on
    # Oracle was the root cause of every cloud bot's
    # ModuleNotFoundError on cold start.
    echo "→ scp core/*.py to /tmp on Oracle"
    local core_files
    core_files=$(ls core/*.py)
    eval "$SCP $core_files $ORACLE_USER@$ORACLE_HOST:/tmp/"
    echo "→ move into /opt/apex_bots/core/ with sudo"
    local core_dest_files
    core_dest_files=""
    for f in core/*.py; do
        core_dest_files="$core_dest_files /tmp/$(basename "$f")"
    done
    run_ssh "sudo mkdir -p /opt/apex_bots/core && \
             sudo cp $core_dest_files /opt/apex_bots/core/ && \
             rm $core_dest_files && \
             (sudo test -f /opt/apex_bots/core/__init__.py || \
              sudo bash -c 'echo \"\" > /opt/apex_bots/core/__init__.py') && \
             sudo chown -R apex:apex /opt/apex_bots/core/"

    # V4.6.40 — also sync the built-in bot scripts to /opt/apex_bots root.
    # bot_runner spawns them as `import longbot_v2/shortbot_v2/daybot`. They
    # were previously deployed once (stale at v4.6.27); shortbot_v2 has since
    # been refactored onto the broker abstraction (IBKR support), so they
    # must be re-pushed or cloud SHORT keeps trading the old way.
    echo "→ scp built-in bot scripts to /tmp on Oracle"
    local bot_scripts="longbot_v2.py shortbot_v2.py daybot.py"
    eval "$SCP $bot_scripts $ORACLE_USER@$ORACLE_HOST:/tmp/"
    echo "→ move into /opt/apex_bots/ with sudo"
    local bot_dest_files
    bot_dest_files=""
    for f in $bot_scripts; do
        bot_dest_files="$bot_dest_files /tmp/$(basename "$f")"
    done
    run_ssh "sudo cp $bot_dest_files /opt/apex_bots/ && \
             rm $bot_dest_files && \
             sudo chown apex:apex /opt/apex_bots/*.py"
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
