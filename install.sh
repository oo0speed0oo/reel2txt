#!/usr/bin/env bash
# One-time install for Ubuntu. Run from inside the reel2txt folder:  bash install.sh
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

echo "== 1/4 system packages (asks for your password) =="
sudo apt update
sudo apt install -y python3-venv ffmpeg tesseract-ocr tesseract-ocr-jpn tesseract-ocr-eng

echo "== 2/4 python environment (a few GB, takes several minutes) =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "== 3/4 start-on-boot service =="
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/reel2txt.service" <<UNIT
[Unit]
Description=reel2txt web app
After=network-online.target

[Service]
EnvironmentFile=$HERE/config.env
ExecStart=$HERE/run.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now reel2txt
sudo loginctl enable-linger "$USER"   # keeps it running at boot, even when nobody is logged in

echo "== 4/4 done =="
IP=$(command -v tailscale >/dev/null && tailscale ip -4 2>/dev/null | head -1 || true)
echo "Open in Chrome:  http://${IP:-YOUR-TAILSCALE-ADDRESS}:8090"
echo "Check it with:   systemctl --user status reel2txt --no-pager"
