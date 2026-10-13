#!/usr/bin/env bash
# Instala el bot como servicio en un VPS Ubuntu/Debian.
# Uso (como root, desde la carpeta del bot): bash deploy/install_vps.sh
set -euo pipefail
DEST=/opt/iphone-stock-bot

apt-get update -y
apt-get install -y python3 python3-venv rsync

id stockbot &>/dev/null || useradd --system --home "$DEST" --shell /usr/sbin/nologin stockbot
mkdir -p "$DEST"
rsync -a --exclude .venv --exclude .git ./ "$DEST"/

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

for f in config.yaml .env proxies.txt; do
  [ -f "$DEST/$f" ] || echo "AVISO: falta $DEST/$f (copia el .example y edítalo)"
done

chown -R stockbot:stockbot "$DEST"
chmod 600 "$DEST/.env" "$DEST/proxies.txt" 2>/dev/null || true

cp "$DEST/deploy/stockbot.service" /etc/systemd/system/stockbot.service
systemctl daemon-reload
systemctl enable --now stockbot
echo
echo "Listo. Ver logs con:  journalctl -u stockbot -f"
