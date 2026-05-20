#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  APEX Trading Platform — Oracle Cloud (Ubuntu 22.04) Server Setup
#  Run as: bash setup_oracle.sh   (as ubuntu user with sudo)
# ═══════════════════════════════════════════════════════════════════════════════
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   APEX Auth Server — Oracle Cloud Setup      ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "▸ Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
sudo apt-get install -y -qq python3.11 python3.11-pip python3.11-venv \
     python3-pip git curl iptables-persistent netfilter-persistent

# ── 2. Create apex user ───────────────────────────────────────────────────────
echo "▸ Creating apex system user..."
sudo useradd -m -s /bin/bash apex 2>/dev/null || echo "  (user 'apex' already exists)"

APEX_HOME=/home/apex
APEX_DATA=$APEX_HOME/apex_data
APEX_SERVER=$APEX_HOME/apex_server
APEX_VENV=$APEX_HOME/apex_venv

sudo mkdir -p "$APEX_DATA"
sudo chown apex:apex "$APEX_DATA"

# ── 3. Copy server files ──────────────────────────────────────────────────────
echo "▸ Copying server files..."
sudo cp -r "$REPO_ROOT/server" "$APEX_SERVER"
sudo chown -R apex:apex "$APEX_SERVER"

# ── 4. Create Python venv + install deps ─────────────────────────────────────
echo "▸ Creating Python virtual environment..."
sudo -u apex python3.11 -m venv "$APEX_VENV"
sudo -u apex "$APEX_VENV/bin/pip" install --quiet --upgrade pip
sudo -u apex "$APEX_VENV/bin/pip" install --quiet -r "$APEX_SERVER/requirements.txt"

# ── 5. Configure secrets ──────────────────────────────────────────────────────
echo ""
echo "  Server configuration"
echo "  ─────────────────────────────────────────────"
read -rp "  JWT secret (press Enter to auto-generate): " JWT_SECRET
if [ -z "$JWT_SECRET" ]; then
    JWT_SECRET=$(python3.11 -c "import secrets; print(secrets.token_hex(32))")
    echo "  Generated: $JWT_SECRET"
fi
echo ""

ENV_FILE="$APEX_DATA/.env"
sudo tee "$ENV_FILE" > /dev/null <<EOF
APEX_JWT_SECRET=$JWT_SECRET
APEX_DB_PATH=$APEX_DATA/apex_server.db
EOF
sudo chown apex:apex "$ENV_FILE"
sudo chmod 600 "$ENV_FILE"

# ── 6. Install systemd service ────────────────────────────────────────────────
echo "▸ Installing systemd service..."
sudo cp "$SCRIPT_DIR/apex_server.service" /etc/systemd/system/

# Patch paths in service file to match actual APEX_HOME
sudo sed -i "s|/home/apex|$APEX_HOME|g" /etc/systemd/system/apex_server.service

sudo systemctl daemon-reload
sudo systemctl enable apex_server
sudo systemctl start  apex_server

# ── 7. Firewall — open port 8000 ─────────────────────────────────────────────
echo "▸ Opening port 8000..."
sudo iptables -C INPUT -p tcp --dport 8000 -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 22   -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT -p tcp --dport 22   -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

# Also open Oracle Cloud's own security-list port (reminder)
echo ""
echo "  ⚠  Remember to open port 8000 in the Oracle Cloud Console:"
echo "     Networking → VCN → Security Lists → Add Ingress Rule"
echo "     Source: 0.0.0.0/0  |  TCP  |  Destination port: 8000"

# ── 8. Verify ─────────────────────────────────────────────────────────────────
sleep 2
sudo systemctl --no-pager status apex_server | head -5

PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<your-oracle-ip>")

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   APEX Server is running!                    ║"
echo "  ║                                              ║"
echo "  ║   URL    : http://$PUBLIC_IP:8000"
echo "  ║   Health : http://$PUBLIC_IP:8000/health"
echo "  ║   API docs: http://$PUBLIC_IP:8000/docs      ║"
echo "  ║                                              ║"
echo "  ║   In the desktop app:                        ║"
echo "  ║   Login window → Configure → paste URL       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  Manage the service:"
echo "    sudo systemctl status  apex_server"
echo "    sudo systemctl restart apex_server"
echo "    sudo journalctl -u apex_server -f"
echo ""
