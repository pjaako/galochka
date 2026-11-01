#!/usr/bin/env bash
# Idempotent installer for galochka on Debian 13. Run as root from the repo
# checkout: sudo bash deploy/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR=/opt/galochka
DATA_DIR=/var/lib/galochka

apt-get update
apt-get install -y python3 python3-venv python3-pip git rsync sqlite3 sudo

if ! id galochka >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin galochka
fi

mkdir -p "$APP_DIR"
if [ "$REPO_DIR" != "$APP_DIR" ]; then
    # First install (or run from a fresh clone elsewhere): copy the checkout
    # into place, keeping .git so /opt/galochka stays a git repo for
    # `git pull` on future updates.
    rsync -a --delete --exclude .venv --exclude data --exclude .superpowers \
        "$REPO_DIR"/ "$APP_DIR"/
fi
# else: already running from /opt/galochka (the "update" workflow, after
# `git pull`) - nothing to sync, the checkout is already in place.

mkdir -p "$DATA_DIR"
chown -R galochka:galochka "$DATA_DIR"

# The venv and the rest of /opt/galochka stay root-owned by design; the
# galochka service only needs to read them, and only ever writes under
# GALOCHKA_DATA (/var/lib/galochka).
if [ ! -d "$APP_DIR/.venv" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
(cd "$APP_DIR" && "$APP_DIR/.venv/bin/pip" install -e .)

FIRST_INSTALL=0
if [ ! -f /etc/galochka.env ]; then
    FIRST_INSTALL=1
    PASSWORD=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)
    cat > /etc/galochka.env <<EOF_ENV
GALOCHKA_DATA=$DATA_DIR
GALOCHKA_PORT=8081
GALOCHKA_EXPERT_PASSWORD=$PASSWORD
GALOCHKA_CASSANDRA_URL=http://cassandra.local:8080
EOF_ENV
    chmod 0640 /etc/galochka.env
    chown root:galochka /etc/galochka.env
fi

cp "$APP_DIR/deploy/galochka.service" /etc/systemd/system/galochka.service
install -m 0755 "$APP_DIR/deploy/galochka-cli" /usr/local/bin/galochka-cli
systemctl daemon-reload
systemctl enable --now galochka
systemctl restart galochka

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$IP" ]; then
    IP="$(hostname)"
fi
echo "galochka is running at: http://$IP:8081"
if [ "$FIRST_INSTALL" -eq 1 ]; then
    PW="$(grep -E '^GALOCHKA_EXPERT_PASSWORD=' /etc/galochka.env | cut -d= -f2-)"
    echo "Generated expert password: $PW"
fi
