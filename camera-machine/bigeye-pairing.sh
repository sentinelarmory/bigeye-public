#!/bin/bash
# Temporary pairing mode for the camera network on the camera machine.
#   sudo bash ~/bigeye-pairing.sh on           open a logged way out (not to the home LAN)
#   sudo bash ~/bigeye-pairing.sh on <MAC>     the same, but for that one device only
#   sudo bash ~/bigeye-pairing.sh off    dead network again
#   bash ~/bigeye-pairing.sh log         show what the devices contacted
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
on)
	[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
	only="${2:-}"
	if [ -n "$only" ]; then
		# Only this one device (MAC address) gets a way out; everything else stays dead.
		echo "$only" | grep -Eqi '^([0-9a-f]{2}:){5}[0-9a-f]{2}$' || { echo "Not a MAC address: $only"; exit 1; }
		tmp=$(mktemp)
		sed "s|^\t\tiifname \"wlp5s0\" ct state new log prefix \"BigEye-OUT \" accept|\t\tiifname \"wlp5s0\" ether saddr != $only counter drop\n&|" "$here/bigeye-pairing.nft" > "$tmp"
		grep -q "ether saddr != $only" "$tmp" || { echo "Could not build the rule."; rm -f "$tmp"; exit 1; }
		nft -f "$tmp"; rm -f "$tmp"
		echo "PAIRING MODE ON for $only ONLY: every other device on the camera Wi-Fi stays dead."
	else
		# Re-pairing a camera: the cameras must reach the real cloud, so their
		# fake-cloud rules are left out (the 360 picture stops until "off").
		tmp=$(mktemp)
		sed '/\$cam360\|\$iwfcams/d' "$here/bigeye-pairing.nft" > "$tmp"
		nft -f "$tmp"; rm -f "$tmp"
		echo "PAIRING MODE ON: camera Wi-Fi can reach the internet, everything is logged."
	fi
	echo "Turn it off with: sudo bash ~/bigeye-pairing.sh off"
	;;
off)
	[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
	systemctl restart bigeye-firewall.service
	echo "Dead network restored."
	;;
log)
	journalctl -k -b --no-pager | grep -E "BigEye-(OUT|DNS)" \
		| sed -E 's/.*(BigEye-[A-Z]+) .*SRC=([^ ]+) DST=([^ ]+) .*PROTO=([^ ]+)( SPT=[0-9]+)? ?(DPT=([0-9]+))?.*/\1 \2 -> \3 \4 \7/' \
		| sort | uniq -c | sort -rn
	;;
*)
	echo "Usage: bigeye-pairing.sh on|off|log"; exit 1 ;;
esac
