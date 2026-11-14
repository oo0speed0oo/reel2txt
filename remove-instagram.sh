#!/usr/bin/env bash
# Deletes the stored Instagram login and session from this box.
rm -f "$HOME/.reel2txt-secrets" "$HOME/reel2txt-data/ig-session-cookies.txt"
systemctl --user restart reel2txt
echo "Instagram login removed."
