#!/usr/bin/env bash
set -euo pipefail
sudo apt-get update
sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 policykit-1
APP_DIR="${HOME}/.local/share/script-studio"
mkdir -p "$APP_DIR" "${HOME}/.local/share/applications"
source_dir="$(dirname "$(realpath "$0")")"
install -m 755 "$source_dir/script_studio.py" "$APP_DIR/script_studio.py"
install -m 644 "$source_dir/USER-GUIDE.txt" "$source_dir/changelog.md" "$APP_DIR/"
install -Dm644 "$source_dir/org.example.ScriptStudio.svg" \
    "${HOME}/.local/share/icons/hicolor/scalable/apps/org.example.ScriptStudio.svg"
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -ft "${HOME}/.local/share/icons/hicolor"
fi
cat > "${HOME}/.local/share/applications/script-studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Script Studio
Comment=Batch manage and run Ubuntu shell scripts
Exec=/usr/bin/python3 "${APP_DIR}/script_studio.py"
Icon=org.example.ScriptStudio
StartupWMClass=org.example.ScriptStudio
Terminal=false
Categories=Utility;System;
EOF
echo 'Script Studio installed. Search "Script Studio" in the application menu.'
echo "Or run: python3 $APP_DIR/script_studio.py"
