#!/bin/bash
# Flash + reset logiciel (watchdog) + capture.
# La carte Super Mini n'expose pas de ligne RTS reliee a EN : esptool la laisse en mode
# telechargement apres le flash, et l'application ne demarre pas. On tente donc un reset par
# chien-de-garde (verifie : l'application demarre), puis on capture.
#
# ⚠️ Le port est detecte dynamiquement : apres un reset la carte se re-enumere et le noyau peut
# lui donner /dev/ttyACM1 au lieu de ttyACM0. Un port code en dur = capture muette en silence.
ESP="$HOME/esptool-venv/bin/python -m esptool"
BIN="${BIN:-$HOME/s002-bridge-v1.bin}"   # image fusionnee complete (debut 0x0)
DUREE="${1:-120}"

trouver_port() {
    for motif in "/dev/serial/by-id/*Espressif*" "/dev/ttyACM*"; do
        for p in $motif; do
            [ -e "$p" ] && { echo "$p"; return; }
        done
    done
}

PORT=$(trouver_port)
if [ -z "$PORT" ]; then
    echo "  AUCUN PORT SERIE : la carte n'est pas detectee (cable ? alimentation ?)"
    exit 1
fi
echo "  port detecte : $PORT"

echo "=== 1. flash avec reset par watchdog ==="
$ESP --chip esp32c3 -p "$PORT" -b 460800 --before default_reset --after watchdog_reset \
     write_flash 0x0 "$BIN" 2>&1 | grep -vE "^Writing at" | tail -8

echo
echo "=== 2. capture $DUREE s (battement de coeur toutes les 5 s) ==="
$HOME/esptool-venv/bin/python "$HOME/capture_loop.py" auto "$DUREE"
