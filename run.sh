#!/usr/bin/env bash
# Starts reel2txt using its own Python environment. Used by the systemd service.
cd "$(dirname "$0")"
source venv/bin/activate
# let the speech model find the NVIDIA libraries installed inside the venv
NV=$(python - <<'PY'
import glob, os, site
dirs = []
for sp in site.getsitepackages():
    dirs += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
print(":".join(dirs))
PY
)
export LD_LIBRARY_PATH="$NV:${LD_LIBRARY_PATH:-}"
exec python app.py
