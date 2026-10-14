#!/bin/bash
# Stops the live engine and removes the background service. The hourly GitHub check takes over again within 30 minutes.
PLIST="$HOME/Library/LaunchAgents/com.solchain.live.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Stopped. Your RPC URL is still in ~/.solchain.env; delete that file if you no longer need it."
