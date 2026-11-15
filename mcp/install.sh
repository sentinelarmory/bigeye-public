#!/usr/bin/env bash
# sentinel-bigeye installer: creates the Python venv the FastMCP server runs in.
# Needs the Sentinel-BigEye page running on this machine (http://127.0.0.1:8770, env BIGEYE_URL).
set -euo pipefail
SRV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m venv "${SRV}/.venv"
"${SRV}/.venv/bin/pip" install --quiet --upgrade pip
"${SRV}/.venv/bin/pip" install --quiet -r "${SRV}/requirements.txt"
echo "==> venv ready -> ${SRV}/.venv/bin/python. Register in ~/.mcp.json, then restart the MCP host."
