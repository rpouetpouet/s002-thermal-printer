#!/bin/bash
# Provisionnement Wi-Fi du noeud d'impression S002 — a lancer UNE fois, sur la machine qui a
# la carte branchee en USB.
#
# Pourquoi ce script plutot qu'un mot de passe compile dans le firmware : un identifiant Wi-Fi
# en dur finit dans l'image binaire, dans l'historique de compilation et dans chaque sauvegarde.
# Ici le mot de passe est saisi dans VOTRE terminal (sans echo) et ecrit directement sur le port
# serie ; le noeud le stocke en NVS. Il ne transite ni par un chat, ni par un depot, ni par un
# fichier de configuration.
#
# Usage :  ~/provisionner_wifi.sh
set -u

PYTHON="${PYTHON:-$HOME/esptool-venv/bin/python}"

trouver_port() {
    for motif in "/dev/serial/by-id/*Espressif*" "/dev/ttyACM*"; do
        for p in $motif; do
            [ -e "$p" ] && { echo "$p"; return; }
        done
    done
}

PORT=$(trouver_port)
if [ -z "$PORT" ]; then
    echo "  AUCUN PORT SERIE : la carte n'est pas detectee (cable USB donnees ? alimentation ?)"
    exit 1
fi
echo "  carte detectee sur $PORT"
echo

read -r -p "  SSID du Wi-Fi  : " SSID
if [ -z "$SSID" ]; then echo "  SSID vide : abandon"; exit 1; fi
read -r -s -p "  Mot de passe   : " PASS
echo

"$PYTHON" - "$PORT" "$SSID" "$PASS" <<'PY'
import sys, time, serial

port, ssid, motdepasse = sys.argv[1], sys.argv[2], sys.argv[3]

# dtr/rts : on ouvre en laissant la carte en mode « run » (sinon elle reste en telechargement).
with serial.Serial(port, 115200, timeout=1, dsrdtr=False, rtscts=False) as serie:
    serie.dtr = True
    serie.rts = False
    time.sleep(0.5)
    serie.reset_input_buffer()

    # On verifie que le firmware repond AVANT d'envoyer quoi que ce soit : ecrire dans le vide
    # donnerait un faux succes.
    serie.write(b"INFO\n")
    reponse = serie.read(4096).decode("utf-8", "replace")
    if "etat" not in reponse:
        print("  Le noeud ne repond pas a INFO (firmware v1 flashe ? carte en mode telechargement ?)")
        print("  --- ce qui a ete lu ---")
        print(reponse[-1500:] or "  (rien)")
        sys.exit(2)

    serie.reset_input_buffer()
    serie.write(f"WIFI {ssid} {motdepasse}\n".encode())
    time.sleep(1.0)
    retour = serie.read(8192).decode("utf-8", "replace")

print("  --- reponse du noeud ---")
for ligne in retour.splitlines():
    if "provisionnement" in ligne or "Wi-Fi" in ligne or "adresse IP" in ligne:
        print("  " + ligne.strip())
print()
if "adresse IP" in retour:
    print("  ✅ Wi-Fi provisionne ET adresse IP obtenue.")
else:
    print("  ⚠️ Enregistre, mais pas d'adresse IP vue : verifier le SSID / mot de passe,")
    print("     ou rebrancher la carte et relancer ce script.")
PY
