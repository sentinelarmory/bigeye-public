#!/bin/bash
# Installs the BigEye camera-network firewall on the camera machine and enables it at boot.
# Run:  sudo bash ~/bigeye-firewall.sh        Undo:  sudo bash ~/bigeye-firewall.sh remove
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = remove ]; then
	systemctl disable --now bigeye-firewall.service 2>/dev/null || true
	rm -f /etc/systemd/system/bigeye-firewall.service /etc/bigeye-firewall.nft
	systemctl daemon-reload
	echo "BigEye firewall removed."
	exit 0
fi

install -m 644 "$here/bigeye-firewall.nft" /etc/bigeye-firewall.nft
cat > /etc/systemd/system/bigeye-firewall.service <<'UNIT'
[Unit]
Description=BigEye camera network firewall
Wants=network-pre.target
Before=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/bigeye-firewall.nft
ExecStop=/usr/sbin/nft delete table inet bigeye

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable bigeye-firewall.service >/dev/null
systemctl restart bigeye-firewall.service
# Bring the camera Wi-Fi up by itself after every start.
if nmcli -t -f NAME connection show | grep -qx Hotspot; then
	nmcli connection modify Hotspot connection.autoconnect yes
	echo "Camera Wi-Fi 'Hotspot' now starts automatically at boot."
fi
echo "BigEye firewall is active:"
nft list table inet bigeye
