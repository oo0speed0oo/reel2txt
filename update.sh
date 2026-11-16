#!/usr/bin/env bash
# Update an existing reel2txt install to v2. Run from inside the reel2txt folder:  bash update.sh
set -e
cd "$(dirname "$0")"
test -x venv/bin/pip || { echo "No venv found here. Run this inside your reel2txt folder."; exit 1; }
echo "== 1/3 installing the new helper (gallery-dl) =="
./venv/bin/pip install -r requirements.txt
echo "== updating the YouTube helper =="
./venv/bin/pip install -U "yt-dlp[default]" -q || echo "(could not update yt-dlp)"
if command -v deno >/dev/null 2>&1 || command -v node >/dev/null 2>&1; then echo "JavaScript runtime found (YouTube needs one)"; else echo "NOTE: YouTube works best with Node.js installed. Run once: sudo apt install -y nodejs"; fi
echo "== installing the page-to-PDF browser (one time, about 150 MB) =="
./venv/bin/playwright install chromium || echo "(browser download failed; plain pages still work. Run: ./venv/bin/playwright install chromium)"
./venv/bin/python - <<'PY' || echo "NOTE: the browser needs system libraries. Run once:  sudo ./venv/bin/playwright install-deps chromium"
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()
print("page-to-PDF browser OK")
PY
echo "== 2/3 adding the new setting to config.env if missing =="
grep -q R2T_ALLOW_THIRD_PARTY config.env || printf '\n# For X posts: if X itself refuses, also ask fxtwitter.com (a third party). 0 = no, 1 = yes\nR2T_ALLOW_THIRD_PARTY=0\n' >> config.env
chmod +x set-instagram.sh remove-instagram.sh 2>/dev/null || true
grep -q R2T_NIGHT_MODEL config.env || printf '\n# Nightly review with a bigger model (see /night). Empty = off. Needs about 9 GB of disk for the download.\nR2T_NIGHT_MODEL=qwen2.5:14b\nR2T_NIGHT_HOUR=0\n' >> config.env
echo "== 3/3 restarting =="
systemctl --user restart reel2txt
sleep 3
systemctl --user status reel2txt --no-pager | head -5
PORT=$(grep -E '^R2T_PORT=' config.env | cut -d= -f2)
echo "Open: http://YOUR-TAILSCALE-IP:${PORT:-8090}   (sorting page: /apply, projects: /edit-projects, categories: /edit-categories)"
