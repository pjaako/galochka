#!/usr/bin/env bash
# Backs up the galochka database and photos to a tar.gz archive in
# <target_dir>, keeping only the 8 newest archives there.
set -euo pipefail
# Archives contain the database (badge tokens): readable by the owner only.
umask 077

TARGET="${1:?usage: backup.sh <target_dir>}"

DATA="${GALOCHKA_DATA:-}"
if [ -z "$DATA" ]; then
    DATA=/var/lib/galochka
    if [ -f /etc/galochka.env ]; then
        ENV_DATA=$(grep -E '^GALOCHKA_DATA=' /etc/galochka.env | cut -d= -f2-)
        if [ -n "$ENV_DATA" ]; then
            DATA="$ENV_DATA"
        fi
    fi
fi

mkdir -p "$TARGET"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

sqlite3 "$DATA/galochka.db" ".backup '$TMP/galochka.db'"

ARCHIVE="$TARGET/galochka-$(date -u +%F).tar.gz"
tar -czf "$ARCHIVE" -C "$TMP" galochka.db -C "$DATA" photos

# keep only the newest 8 archives
ls -1 "$TARGET"/galochka-*.tar.gz | sort | head -n -8 | xargs -r rm --

echo "$ARCHIVE"
