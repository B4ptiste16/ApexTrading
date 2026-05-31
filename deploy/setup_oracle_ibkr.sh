#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  APEX — Oracle Cloud: provision headless IB Gateway (paper, 24/7)
# ───────────────────────────────────────────────────────────────────────────────
#  Installs everything server/ibkr_gateway.py needs to launch a per-user IB
#  Gateway under Xvfb + IBC (IBController), so IBKR bots can run on the cloud
#  WITHOUT the user keeping TWS/Gateway open on their laptop.
#
#  Run on the Oracle box (opc@145.241.170.165) as a sudo-capable user:
#     bash setup_oracle_ibkr.sh
#
#  Idempotent — re-running is safe (skips already-installed pieces).
#
#  Installs to the paths server/ibkr_gateway.py defaults to:
#     IB Gateway  → /opt/ibgateway   (APEX_TWS_PATH)
#     IBC         → /opt/ibc         (APEX_IBC_PATH)
#     Xvfb        → system package
#  Per-user gateway settings live under /opt/apex_users/user_<id>/ibgateway
#  (created on demand by ibkr_gateway.py, owned by the 'apex' service user).
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Versions (override via env) ────────────────────────────────────────────────
IBGW_CHANNEL="${APEX_IBGW_CHANNEL:-stable}"      # stable | latest
IBC_VERSION="${APEX_IBC_VERSION:-3.23.0}"
TWS_PATH="${APEX_TWS_PATH:-/opt/ibgateway}"
IBC_PATH="${APEX_IBC_PATH:-/opt/ibc}"
APEX_USER="${APEX_SERVICE_USER:-apex}"
STATE_ROOT="${APEX_IBKR_GW_STATE:-/opt/apex_users}"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   APEX — headless IB Gateway provisioning    ║"
echo "  ╚══════════════════════════════════════════════╝"
echo "    IB Gateway → $TWS_PATH  (channel: $IBGW_CHANNEL)"
echo "    IBC        → $IBC_PATH  (v$IBC_VERSION)"
echo ""

# ── 1. Xvfb + X libs (IB Gateway is a Java GUI app even headless) ───────────────
# Oracle Linux 9 / RHEL — use dnf. There is NO `xvfb-run` here (that's a Debian
# script); we install the Xvfb server binary and ibkr_gateway.py drives it
# directly. The X libraries below are what the bundled Java AWT/Swing needs.
echo "▸ Installing Xvfb + X libraries (dnf)..."
sudo dnf install -y -q \
    xorg-x11-server-Xvfb xorg-x11-xauth unzip curl \
    libXtst libXrender libXi libXext libX11 libXScrnSaver \
    libXrandr libXcursor libXcomposite libXdamage libXfixes \
    freetype fontconfig dejavu-sans-fonts gtk3 2>&1 | tail -3 || true

# ── 2. IB Gateway (bundled JRE — no separate Java install needed) ──────────────
if [ -x "$TWS_PATH/ibgateway" ] || ls "$TWS_PATH"/*/ibgateway >/dev/null 2>&1; then
    echo "▸ IB Gateway already present at $TWS_PATH — skipping download."
else
    echo "▸ Downloading IB Gateway ($IBGW_CHANNEL channel)..."
    INSTALLER=/tmp/ibgateway-installer.sh
    curl -fsSL \
      "https://download2.interactivebrokers.com/installers/ibgateway/${IBGW_CHANNEL}-standalone/ibgateway-${IBGW_CHANNEL}-standalone-linux-x64.sh" \
      -o "$INSTALLER"
    chmod +x "$INSTALLER"
    echo "▸ Installing IB Gateway to $TWS_PATH (unattended)..."
    # InstallBuilder unattended flags. The installer bundles its own JRE.
    sudo "$INSTALLER" -q -dir "$TWS_PATH" || {
        echo "  ! unattended install returned non-zero — check $TWS_PATH" >&2
    }
    rm -f "$INSTALLER"
fi

# The standalone installer lays files FLAT in $TWS_PATH (jars at
# $TWS_PATH/jars). Detect the version from the desktop launcher / vmoptions.
DETECTED_VER="$(ls "$TWS_PATH" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
[ -z "$DETECTED_VER" ] && DETECTED_VER="$(ls "$TWS_PATH"/'IB Gateway '*.desktop 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
[ -z "$DETECTED_VER" ] && DETECTED_VER="10.45"
echo "  detected IB Gateway version: $DETECTED_VER"

# IBC (gateway mode) looks for jars at  <launch-root>/ibgateway/<version>/jars
# but the installer put them flat at $TWS_PATH/jars. Build the layout IBC
# expects in a clean launch root via symlink (no file moves / no reinstall).
LAUNCH_ROOT="${APEX_IBKR_LAUNCH_ROOT:-/opt/ibkr_tws}"
echo "▸ Creating IBC launch layout: $LAUNCH_ROOT/ibgateway/$DETECTED_VER -> $TWS_PATH"
sudo mkdir -p "$LAUNCH_ROOT/ibgateway"
sudo ln -sfn "$TWS_PATH" "$LAUNCH_ROOT/ibgateway/$DETECTED_VER"

# ── 3. IBC (IBController) — automates the Gateway login ────────────────────────
if [ -f "$IBC_PATH/scripts/ibcstart.sh" ]; then
    echo "▸ IBC already present at $IBC_PATH — skipping download."
else
    echo "▸ Downloading IBC v$IBC_VERSION..."
    IBC_ZIP=/tmp/ibc.zip
    curl -fsSL \
      "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip" \
      -o "$IBC_ZIP"
    sudo mkdir -p "$IBC_PATH"
    sudo unzip -o -q "$IBC_ZIP" -d "$IBC_PATH"
    sudo chmod +x "$IBC_PATH"/*.sh "$IBC_PATH"/scripts/*.sh 2>/dev/null || true
    rm -f "$IBC_ZIP"
fi

# ── 4. Permissions — service user owns the install + per-user state ────────────
echo "▸ Setting ownership for service user '$APEX_USER'..."
id "$APEX_USER" >/dev/null 2>&1 || echo "  (user '$APEX_USER' not found — adjust APEX_SERVICE_USER)"
sudo mkdir -p "$STATE_ROOT"
sudo chown -R "$APEX_USER":"$APEX_USER" "$STATE_ROOT" 2>/dev/null || true
# IBC + Gateway are read-only at runtime; just make them readable/executable.
sudo chmod -R a+rX "$IBC_PATH" "$TWS_PATH" 2>/dev/null || true

# ── 5. Verify the pieces ibkr_gateway.py checks in _preflight() ────────────────
echo ""
echo "▸ Preflight verification:"
command -v Xvfb >/dev/null 2>&1 \
    && echo "  ✓ Xvfb on PATH" \
    || echo "  ✗ Xvfb MISSING (dnf install xorg-x11-server-Xvfb)"
[ -f "$IBC_PATH/scripts/ibcstart.sh" ] \
    && echo "  ✓ IBC at $IBC_PATH/scripts/ibcstart.sh" \
    || echo "  ✗ IBC ibcstart.sh MISSING"
[ -e "$LAUNCH_ROOT/ibgateway/$DETECTED_VER/jars" ] \
    && echo "  ✓ jars reachable at $LAUNCH_ROOT/ibgateway/$DETECTED_VER/jars" \
    || echo "  ✗ jars NOT reachable via launch root"

# ── 6. Persist the env the service needs (idempotent) ──────────────────────────
ENV_FILE="${APEX_SERVER_ENV:-/opt/apex_data/.env}"
echo "▸ Writing APEX_TWS_PATH / APEX_TWS_VERSION into $ENV_FILE"
set_env() {  # key value
    if sudo grep -q "^$1=" "$ENV_FILE" 2>/dev/null; then
        sudo sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"
    else
        echo "$1=$2" | sudo tee -a "$ENV_FILE" >/dev/null
    fi
}
set_env APEX_TWS_PATH    "$LAUNCH_ROOT"
set_env APEX_TWS_VERSION "$DETECTED_VER"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   IB Gateway provisioning complete           ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  APEX_TWS_PATH=$LAUNCH_ROOT  APEX_TWS_VERSION=$DETECTED_VER  (written to $ENV_FILE)"
echo "  Restart the service to apply:  sudo systemctl restart apex_server.service"
echo ""
echo "  The auth service spawns one gateway per user on first IBKR bot start;"
echo "  watch a launch with:"
echo "     sudo tail -f $STATE_ROOT/user_<id>/ibgateway/gateway.log"
echo ""
