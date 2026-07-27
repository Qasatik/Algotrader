#!/usr/bin/env bash
# =====================================================================
#  install-autostart.sh — set up the Bybit carry bot to start with the
#  computer, WITHOUT root (user systemd service + XDG autostart entry).
#
#  Both triggers converge on a single user service (systemctl --user start
#  is idempotent), so there is no risk of two processes trading one account.
#
#  Usage:
#     bash deploy/install-autostart.sh            # install + enable + start
#     bash deploy/install-autostart.sh --remove   # disable + remove files
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="carry-bot"

USER_UNIT_DIR="${HOME}/.config/systemd/user"
AUTOSTART_DIR="${HOME}/.config/autostart"

log()  { echo "[install] $*"; }
fail() { echo "[install][ERROR] $*" >&2; exit 1; }

# --- sanity checks ----------------------------------------------------
[ -f "$REPO_DIR/scripts/run_carry_testnet.py" ] \
  || fail "run_carry_testnet.py not found under $REPO_DIR"
[ -f "$REPO_DIR/.env" ] \
  || fail ".env not found under $REPO_DIR (create it from .env.example first)"
command -v systemctl >/dev/null 2>&1 \
  || fail "systemctl not available on this system"

if [ "${1:-}" = "--remove" ]; then
  log "Disabling user service..."
  systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
  log "Removing installed files..."
  rm -f "$USER_UNIT_DIR/$SERVICE_NAME.service" "$AUTOSTART_DIR/$SERVICE_NAME.desktop"
  systemctl --user daemon-reload || true
  log "Removed. (The carry position on the exchange is left as-is.)"
  exit 0
fi

# --- install ----------------------------------------------------------
log "Repo dir: $REPO_DIR"
mkdir -p "$USER_UNIT_DIR" "$AUTOSTART_DIR"

log "Installing user service unit -> $USER_UNIT_DIR/$SERVICE_NAME.service"
# Rewrite %h is already used by the unit; just copy it verbatim.
cp "$SCRIPT_DIR/carry-bot.user.service" "$USER_UNIT_DIR/$SERVICE_NAME.service"

log "Installing XDG autostart entry -> $AUTOSTART_DIR/$SERVICE_NAME.desktop"
cp "$SCRIPT_DIR/carry-bot.desktop" "$AUTOSTART_DIR/$SERVICE_NAME.desktop"

log "Reloading user systemd..."
systemctl --user daemon-reload

log "Enabling + starting the service now (also starts at every login)..."
systemctl --user enable --now "$SERVICE_NAME"

# --- verify -----------------------------------------------------------
sleep 2
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
  log "OK: service is active (running)."
else
  log "WARN: service is not active yet. Recent logs:"
  journalctl --user -u "$SERVICE_NAME" -n 20 --no-pager || true
  fail "Service did not reach active state. Check logs above."
fi

log ""
log "Status:"
systemctl --user status "$SERVICE_NAME" --no-pager || true
log ""
log "Live logs:   journalctl --user -u $SERVICE_NAME -f"
log "Stop:        systemctl --user stop $SERVICE_NAME   (position stays OPEN)"
log ""
log "OPTIONAL — start at boot BEFORE login (needs root, run once):"
log "   sudo loginctl enable-linger $USER"
log "Without linger the bot starts when you log in to the desktop."
