#!/usr/bin/env python3
"""Capture robuste : survit a la disparition/reapparition du port (debranchement physique).

Ouvre en mode « run » (DTR=1, RTS=0 : sur l'USB-Serial-JTAG de la C3, ces lignes ne sont pas
des lignes de modem mais le signal de mode de demarrage), horodate chaque ligne et retente
l'ouverture en boucle pendant toute la duree demandee.
"""
import glob
import os
import sys
import time

import serial


def trouver_port() -> str:
    """Trouve le port du C3 sans le coder en dur.

    ⚠️ Piege vecu : apres un reset, la carte se re-enumere et le noyau peut lui attribuer
    /dev/ttyACM1 au lieu de ttyACM0. Viser ttyACM0 en dur fait echouer toute la capture en
    silence (aucune ligne ouverte). On cherche donc l'identifiant stable Espressif, puis on
    retombe sur le premier ttyACM disponible.
    """
    for motif in ("/dev/serial/by-id/*Espressif*", "/dev/ttyACM*"):
        trouves = sorted(glob.glob(motif))
        if trouves:
            return trouves[0]
    return "/dev/ttyACM0"


port = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "auto" else trouver_port()
duree = int(sys.argv[2]) if len(sys.argv) > 2 else 300

t0 = time.time()
ser = None
print(f"  [capture] debut {time.strftime('%H:%M:%S')} sur {port}, {duree} s", flush=True)

while time.time() - t0 < duree:
    if ser is None:
        if not os.path.exists(port):
            time.sleep(0.5)
            continue
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            ser.dtr = True
            ser.rts = False
            time.sleep(0.2)
            ser.reset_input_buffer()
            print(f"  [capture] {time.strftime('%H:%M:%S')} port ouvert (mode run)", flush=True)
        except Exception as e:
            print(f"  [capture] ouverture impossible : {e}", flush=True)
            ser = None
            time.sleep(1)
            continue

    try:
        ligne = ser.readline()
        if ligne:
            texte = ligne.decode("utf-8", "replace").rstrip()
            print(f"{time.strftime('%H:%M:%S')} {texte}", flush=True)
    except Exception:
        print(f"  [capture] {time.strftime('%H:%M:%S')} port perdu (debranchement ?)", flush=True)
        try:
            ser.close()
        except Exception:
            pass
        ser = None

if ser:
    ser.close()
print("=== fin de capture ===", flush=True)
