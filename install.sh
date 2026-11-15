#!/usr/bin/env bash
# Sentinel BigEye installer (systemd --user, no root needed except for the firewall step it prints).
#
#   ./install.sh [--dry-run] [--force] [--no-start] [--export-models] main|camera|all
#
#   main     the machine with the page and the AI (and a GPU, if you have one)
#   camera   the machine that hosts the camera Wi-Fi, the stand-in clouds and the bridges
#   all      both on one machine (one-machine mode)
#
#   --dry-run        show every step, change nothing
#   --force          overwrite service files that already exist (your settings file is never overwritten)
#   --no-start       install and enable, but do not start the services
#   --export-models  also download and export the detector models (needs ~3 GB for a temporary PyTorch)
#
# Safe to run again: steps that are already done are skipped. After each part it checks that it works.
set -euo pipefail

DRY=0 FORCE=0 START=1 EXPORT=0 ROLE=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;; --force) FORCE=1 ;; --no-start) START=0 ;; --export-models) EXPORT=1 ;;
    main|camera|all) ROLE="$a" ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see --help)"; exit 2 ;;
  esac
done
[ -n "$ROLE" ] || { sed -n '2,16p' "$0"; exit 2; }

REPO="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/bigeye"                                  # the service files expect the code here
CONF="$HOME/.config/sentinel-bigeye"
UNITS="$HOME/.config/systemd/user"
VENV="$HOME/.local/share/sentinel-bigeye/ai-venv"
MODELS="$HOME/.local/share/sentinel-bigeye/models"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   ok: %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '   would run: %s\n' "$*"; else "$@"; fi; }
need() { command -v "$1" >/dev/null 2>&1; }

check_python() {
  need python3 || { warn "python3 is missing (install Python 3.11 or newer)"; exit 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { warn "Python 3.11 or newer is needed (found $(python3 -V))"; exit 1; }
  ok "$(python3 -V)"
}

link_app() {
  say "Code in $APP"
  if [ "$REPO" = "$APP" ] || [ "$(readlink -f "$APP" 2>/dev/null)" = "$REPO" ]; then ok "already here"; return; fi
  if [ -e "$APP" ]; then warn "$APP exists and is not this checkout; move it away or clone the repo there"; exit 1; fi
  run ln -s "$REPO" "$APP"
}

install_unit() {   # install_unit name.service
  local src="$REPO/systemd/$1" dst="$UNITS/$1"
  if [ -e "$dst" ] && [ "$FORCE" = 0 ]; then ok "$1 already installed (use --force to replace)"; return; fi
  run mkdir -p "$UNITS"
  run cp "$src" "$dst"
}

enable_units() {   # enable_units a.service b.service ...
  need systemctl || { warn "no systemctl: start the programs by hand (see docs/setup.md)"; return; }
  run systemctl --user daemon-reload
  if [ "$START" = 1 ]; then run systemctl --user enable --now "$@"; else run systemctl --user enable "$@"; fi
  if need loginctl && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    warn "services stop when you log out; to keep them running: sudo loginctl enable-linger $USER"
  fi
}

check_url() {      # check_url what url
  [ "$DRY" = 1 ] && { ok "would check $2"; return; }
  [ "$START" = 1 ] || return 0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fs -m 3 "$2" >/dev/null 2>&1; then ok "$1 answers ($2)"; return; fi
    sleep 2
  done
  warn "$1 does not answer on $2 yet; see: journalctl --user -u <service> -n 50"
}

main_part() {
  say "Main machine: checks"
  check_python
  need ffmpeg && ok "ffmpeg $(ffmpeg -version | head -1 | cut -d' ' -f3)" || { warn "ffmpeg is missing (sudo apt install ffmpeg)"; exit 1; }
  need uv && ok "uv $(uv --version | cut -d' ' -f2)" || { warn "uv is missing: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
  if need nvidia-smi || [ -x /usr/lib/wsl/lib/nvidia-smi ]; then ok "NVIDIA GPU found"; else warn "no NVIDIA GPU: the AI runs on the processor (slower, still works)"; fi

  say "Settings file"
  if [ -e "$CONF/bigeye.toml" ]; then ok "$CONF/bigeye.toml exists (left as it is)"
  else
    run mkdir -p "$CONF"
    run install -m 600 "$REPO/bigeye.example.toml" "$CONF/bigeye.toml"
    warn "fill in your cameras and folders in $CONF/bigeye.toml (it stays on this machine)"
  fi

  say "AI environment ($VENV)"
  if [ -x "$VENV/bin/python" ]; then ok "exists"; else run uv venv -q -p 3.12 "$VENV"; fi
  run uv pip install -q -p "$VENV/bin/python" "onnxruntime-gpu[cuda,cudnn]" numpy pillow insightface opencv-python-headless psutil
  run uv pip uninstall -q -p "$VENV/bin/python" onnxruntime || true   # insightface pulls the CPU build; keep the GPU one

  say "Detector models ($MODELS)"
  run mkdir -p "$MODELS"
  if ls "$MODELS"/*.onnx >/dev/null 2>&1; then ok "found: $(cd "$MODELS" && ls -- *.onnx | tr '\n' ' ')"
  elif [ "$EXPORT" = 1 ]; then
    local tmp; tmp="$(mktemp -d)"
    run uv venv -q -p 3.12 "$tmp/venv"
    run uv pip install -q -p "$tmp/venv/bin/python" --index-url https://download.pytorch.org/whl/cpu torch torchvision
    run uv pip install -q -p "$tmp/venv/bin/python" rfdetr onnx onnxsim
    run bash -c "cd '$MODELS' && '$tmp/venv/bin/python' -c \"from rfdetr import RFDETRSmall; RFDETRSmall().export(output_dir='.')\""
    run rm -rf "$tmp"
  else
    warn "no model yet: run again with --export-models, or follow docs/setup.md step 6"
  fi

  say "Services (page + AI)"
  install_unit bigeye-ai.service
  install_unit bigeye-view.service
  warn "edit the camera list in $UNITS/bigeye-view.service if your cameras differ from the example"
  enable_units bigeye-ai.service bigeye-view.service
  check_url "AI service" http://127.0.0.1:8771/status
  check_url "BigEye page" http://127.0.0.1:8770/state
}

camera_part() {
  say "Camera machine: checks"
  check_python
  need nft && ok "nftables found" || warn "nftables missing (sudo apt install nftables)"
  need iw && ok "iw found" || warn "iw missing (sudo apt install iw): needed to host the camera Wi-Fi"
  need nmcli && ok "NetworkManager found" || warn "nmcli missing: set up the hotspot your own way (docs/setup.md step 2)"

  say "Stand-in clouds and the 360 bridge"
  install_unit bigeye-fakecloud.service
  install_unit bigeye-iwfcloud.service
  install_unit bigeye-bridge-cam360.service
  enable_units bigeye-fakecloud.service bigeye-iwfcloud.service bigeye-bridge-cam360.service
  check_url "360 bridge" http://127.0.0.1:18767/status

  say "Firewall (needs root; not run by this script)"
  echo "   Edit the placeholder MAC addresses in $APP/camera-machine/bigeye-firewall.nft, then:"
  echo "     sudo $APP/camera-machine/bigeye-firewall.sh"
  echo "   Check: a camera can no longer reach the internet, and the call-home tab starts counting."
  say "Door / window bridge"
  if need go; then
    run bash -c "cd '$APP/bridges/doorbridge' && go build -trimpath -ldflags='-s -w' -o bigeye-doorbridge ."
    install_unit bigeye-bridge-door.service
    warn "fill in the camera address and LAN token in $UNITS/bigeye-bridge-door.service, then: systemctl --user enable --now bigeye-bridge-door"
  else
    warn "Go is missing: install Go 1.23+ to build the door/window bridge (bridges/doorbridge/README.md)"
  fi
}

[ "$DRY" = 1 ] && echo "Dry run: nothing will be changed."
link_app
case "$ROLE" in
  main) main_part ;;
  camera) camera_part ;;
  all) camera_part; main_part ;;
esac
say "Done"
echo "   Open http://localhost:8770 . Setup guide with a check after each step: docs/setup.md"
