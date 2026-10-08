#!/usr/bin/env bash
# Run PON Fleet as a service on a Raspberry Pi / Linux box:  sudo ./install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-pi}"
apt-get update -qq && apt-get install -y -qq python3-flask
timedatectl set-ntp true || true
cat > /etc/systemd/system/ponfleet.service <<UNIT
[Unit]
Description=PON Fleet control server
After=network-online.target

[Service]
User=${USER_NAME}
WorkingDirectory=${DIR}
ExecStart=/usr/bin/python3 ${DIR}/fleet.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
chown -R "${USER_NAME}" "${DIR}/data" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now ponfleet.service
systemctl restart ponfleet.service
echo "PON Fleet is running: http://$(hostname -I | awk '{print $1}'):8090"
