#!/bin/bash
# Install or update Planet Helper from this checkout on Linux (KDE Plasma tested).
# Usage: clone the repository to ~/.local/share/planet-helper, then run ./install-linux.sh
set -eu
app_dir="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$app_dir/.venv/bin/python" ]; then
    "${PLANET_HELPER_PYTHON:-python3}" -m venv "$app_dir/.venv"
fi
"$app_dir/.venv/bin/python" -m pip install --quiet --upgrade pip
"$app_dir/.venv/bin/python" -m pip install --quiet -r "$app_dir/requirements-linux.lock"

if [ "${1:-}" = --prepare ]; then
    QT_QPA_PLATFORM=offscreen "$app_dir/.venv/bin/python" -c 'import sys; sys.path.insert(0, sys.argv[1]); import linux_app' "$app_dir"
    exit 0
fi
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons"
install -m 644 "$app_dir/planet-helper-icon.png" "$HOME/.local/share/icons/planet-helper.png"
cat > "$HOME/.local/bin/planet-helper" <<EOF
#!/bin/bash
set -eu
app_dir="$app_dir"
cd "\$app_dir"
export PYTHONUNBUFFERED=1
exec "\$app_dir/.venv/bin/python" "\$app_dir/linux_updates.py" launch "\$app_dir" "\$@" >>"\$app_dir/launch.log" 2>&1
EOF
chmod +x "$HOME/.local/bin/planet-helper"

cat > "$HOME/.local/share/applications/planet-helper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=플래닛 도우미
Comment=Maple Planet EXP 측정기
Exec=$HOME/.local/bin/planet-helper
Icon=$HOME/.local/share/icons/planet-helper.png
Terminal=false
Categories=Game;Utility;
X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
EOF

echo "설치 완료: 앱 메뉴에서 '플래닛 도우미'를 실행하세요."
