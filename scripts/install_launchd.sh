#!/usr/bin/env bash
# Install the daily collection job on macOS (launchd).
#
#   bash scripts/install_launchd.sh
#
# Schedules scripts/daily_collect.sh at 08:00 local time. If the Mac is asleep
# at 08:00, launchd runs the job on next wake; if it is powered off, that day
# is skipped (a gap the collection-health page will show honestly).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.priceforensics.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/priceforensics.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${REPO}/scripts/daily_collect.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>20</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>${LOG}</string>
    <key>StandardErrorPath</key><string>${LOG}</string>
    <key>WorkingDirectory</key><string>${REPO}</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "installed: ${LABEL}"
echo "schedule : daily 08:00 local time"
echo "log      : ${LOG}"
echo
echo "run now to test:   launchctl start ${LABEL}"
echo "watch the log:     tail -f ${LOG}"
echo "uninstall:         launchctl unload ${PLIST} && rm ${PLIST}"
