#!/bin/bash
# Installs the live paper copy-trading engine as a background service on this Mac.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.solchain.live"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/solchain-live.log"

command -v python3 >/dev/null || { echo "Python 3 is missing. Run: xcode-select --install"; exit 1; }

echo "1/4 Python environment"
python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip websockets

echo "2/4 RPC"
if [ ! -f "$HOME/.solchain.env" ]; then
  read -rsp "Paste your Helius RPC URL (https://mainnet.helius-rpc.com/?api-key=...; input is hidden): " RPC; echo
  (umask 077; printf 'SOL_RPC=%q\n' "$RPC" > "$HOME/.solchain.env")
fi

echo "3/4 GitHub access"
command -v gh >/dev/null && gh auth setup-git >/dev/null 2>&1 || true
git -C "$REPO" config user.name >/dev/null || git -C "$REPO" config user.name "SolChain live engine"
git -C "$REPO" config user.email >/dev/null || git -C "$REPO" config user.email "solchain-live@users.noreply.github.com"
if ! git -C "$REPO" push --dry-run -q origin HEAD:main 2>/dev/null; then
  echo "This Mac can't push to GitHub yet. Run 'gh auth login' (brew install gh), then run this script again."
  exit 1
fi

echo "4/4 Background service"
chmod +x "$REPO/mac/run.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$REPO/mac/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Running. Follow it with: tail -f $LOG"
