#!/bin/bash
# Chief of Staff installer (macOS). Safe to re-run.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
cd "$DIR"
mkdir -p logs snapshots && chmod 700 "$DIR" logs snapshots
[ -f config.json ] || { cp config.example.json config.json; echo "→ created config.json (edit it)"; }
[ -f profile.md ]  || { cp profile.example.md profile.md; echo "→ created profile.md (edit it)"; }
[ -f todos.json ]  || echo '[]' > todos.json
chmod 600 config.json profile.md todos.json

build_app () {  # build_app <name> <source.swift> <bundle-id> <usage-key> <usage-text>
  local app="bin/$1.app"
  rm -rf "$app"; mkdir -p "$app/Contents/MacOS"
  swiftc -O "bin/$2" -o "$app/Contents/MacOS/$1"
  cat > "$app/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$3</string><key>CFBundleName</key><string>$1</string>
  <key>CFBundleExecutable</key><string>$1</string><key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string><key>LSUIElement</key><true/>
  <key>$4</key><string>$5</string>
</dict></plist>
PL
  codesign --force --deep -s - "$app"
}
echo "→ building helper apps (camera + Bluetooth get their own macOS permission prompts)"
build_app RoomCam snap.swift local.chiefofstaff.roomcam NSCameraUsageDescription "Room-check photos you ask for."
build_app BedJetCtl bedjet.swift local.chiefofstaff.bedjet NSBluetoothAlwaysUsageDescription "Controls your BedJet over Bluetooth."
chmod +x bin/bedjet bin/roomcheck bin/text bin/contact bin/studio

LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA"
cat > "$LA/com.chiefofstaff.server.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.chiefofstaff.server</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/server.py</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/server.log</string><key>StandardErrorPath</key><string>$DIR/logs/server.log</string>
</dict></plist>
PL
cat > "$LA/com.chiefofstaff.open.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.chiefofstaff.open</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>-c</string>
    <string>sleep 8; open -na "Google Chrome" --args --app=http://127.0.0.1:8787 --start-fullscreen --user-data-dir=$DIR/.chrome-profile</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL
for a in server open; do
  launchctl bootout "gui/$(id -u)/com.chiefofstaff.$a" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/com.chiefofstaff.$a.plist"
done
echo "✓ Dashboard running at http://127.0.0.1:8787"
echo "  Next: edit config.json + profile.md, then ask Claude to create the hourly sync task (see README)."
