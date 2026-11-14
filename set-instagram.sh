#!/usr/bin/env bash
# Stores the BURNER Instagram login for reel2txt, privately on this box.
# Nothing you type is shown on screen, saved in shell history, or sent anywhere except to Instagram.
set -e
read -r -p "Burner Instagram username: " U
read -r -s -p "Burner Instagram password (typing is hidden): " P
echo
[ -n "$U" ] && [ -n "$P" ] || { echo "Both are needed. Nothing saved."; exit 1; }
umask 077
printf 'IG_USER=%s\nIG_PASS=%s\n' "$U" "$P" > "$HOME/.reel2txt-secrets"
chmod 600 "$HOME/.reel2txt-secrets"
rm -f "$HOME/reel2txt-data/ig-session-cookies.txt"
systemctl --user restart reel2txt
echo "Saved to ~/.reel2txt-secrets (only your user can read it). reel2txt signs in by itself on the next Instagram link"
echo "and renews the session about every 6 days. To remove it later: bash remove-instagram.sh"
