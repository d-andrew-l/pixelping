#!/bin/sh
# Install sysstats.py as a hardened, read-only HTTP stats agent (systemd + nftables).
# Optional: by default pixelping reads stats over ssh and needs nothing installed.
# Run as root on the host to monitor, from a copy of the pixelping repo:
#
#   sudo deploy/install-agent.sh --bind 192.168.1.20 --allow 192.168.1.10 \
#       --mounts "/ /srv" --services "nginx rsyslog"
#
# Then set the host's "stats": {"url": "http://192.168.1.20:9105/stats"} in config.json.
# --dry-run prints the filled-in files instead of installing them.
set -eu

usage() {
	echo "usage: $0 --bind IP --allow \"IP [IP...]\" [--port 9105] [--mounts \"/ ...\"] [--services \"unit ...\"] [--dry-run]" >&2
	exit 2
}

BIND= ALLOW= PORT=9105 MOUNTS=/ SERVICES= DRY=
while [ $# -gt 0 ]; do
	case "$1" in
		--bind) BIND=${2-}; shift 2 ;;
		--allow) ALLOW=${2-}; shift 2 ;;
		--port) PORT=${2-}; shift 2 ;;
		--mounts) MOUNTS=${2-}; shift 2 ;;
		--services) SERVICES=${2-}; shift 2 ;;
		--dry-run) DRY=1; shift ;;
		*) usage ;;
	esac
done
[ -n "$BIND" ] && [ -n "$ALLOW" ] || usage

# strict input checks: values end up in a unit file and a firewall rule
ipv4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
check() {   # check NAME REGEX VALUE...
	name=$1 re=$2; shift 2
	for v in "$@"; do
		printf '%s\n' "$v" | grep -Eq "$re" || { echo "invalid $name: $v" >&2; exit 2; }
	done
}
check --bind "$ipv4" "$BIND"
# shellcheck disable=SC2086  # word splitting is intended for these lists
check --allow "$ipv4" $ALLOW
check --port '^[0-9]{1,5}$' "$PORT"
# shellcheck disable=SC2086
check --mounts '^/[A-Za-z0-9_./-]*$' $MOUNTS
# shellcheck disable=SC2086
[ -z "$SERVICES" ] || check --services '^[A-Za-z0-9@_.:-]+$' $SERVICES

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/stats-agent"
# shellcheck disable=SC2086
ALLOW_NFT=$(echo $ALLOW | sed 's/ /, /g')

fill() {
	sed -e "s|@BIND@|$BIND|g" -e "s|@PORT@|$PORT|g" -e "s|@ALLOW_NFT@|$ALLOW_NFT|g" \
		-e "s|@ALLOW@|$ALLOW|g" -e "s|@MOUNTS@|$MOUNTS|g" -e "s|@SERVICES@|$SERVICES|g" "$1"
}

if [ -n "$DRY" ]; then
	echo "# /etc/systemd/system/pixelping-stats.service"; fill "$SRC/pixelping-stats.service"
	echo; echo "# /opt/pixelping-stats/pixelping-stats.nft"; fill "$SRC/pixelping-stats.nft"
	exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "run as root (or use --dry-run)" >&2; exit 1; }
command -v nft >/dev/null || { echo "nft not found: install nftables" >&2; exit 1; }

id pixelping-stats >/dev/null 2>&1 ||
	useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pixelping-stats
install -d -m 0755 /opt/pixelping-stats
install -m 0644 "$HERE/../sysstats.py" /opt/pixelping-stats/sysstats.py
fill "$SRC/pixelping-stats.nft" > /opt/pixelping-stats/pixelping-stats.nft
fill "$SRC/pixelping-stats.service" > /etc/systemd/system/pixelping-stats.service
systemctl daemon-reload
systemctl enable --now pixelping-stats.service
systemctl restart pixelping-stats.service   # pick up changes on a re-run
echo "installed. From the pixelping server: curl http://$BIND:$PORT/stats"
