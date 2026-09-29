#!/usr/bin/env python3
"""Provisionne le Wi-Fi du noeud S002 en lisant les identifiants DEJA presents sur cette machine.

Pourquoi ce detour : le mot de passe Wi-Fi ne doit transiter ni par un chat, ni par un depot,
ni par un fichier de configuration. Ce script lit le profil NetworkManager actif de la machine
qui heberge la carte (donc un secret deja en place, jamais recopie a la main) et l'ecrit
directement sur le port serie du noeud, qui le stocke en NVS.

Il n'affiche JAMAIS le mot de passe : seules les lignes de journal du firmware qui concernent le
Wi-Fi et l'adresse IP sont restituees (le firmware ne journalise que le SSID, pas le secret).
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

AFFICHER_LIGNES = ("provisionnement", "Wi-Fi", "wi-fi", "adresse IP", "console", "aucun SSID")


def trouver_port():
    """Port de la carte, detecte dynamiquement (ttyACM0 ou ttyACM1 selon le reset)."""
    for motif in ("/dev/serial/by-id/*Espressif*", "/dev/ttyACM*"):
        for candidat in sorted(glob.glob(motif)):
            if os.path.exists(candidat):
                return candidat
    return None


def identifiants_systeme():
    """(ssid, mot de passe) du profil Wi-Fi actif de CETTE machine. Leve si introuvable."""
    profils = subprocess.run(
        ["sudo", "-n", "nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        capture_output=True, text=True,
    )
    nom = None
    for ligne in profils.stdout.splitlines():
        if ":" in ligne and "wireless" in ligne:
            nom = ligne.split(":", 1)[0]
            break

    fichiers = []
    if nom:
        fichiers += glob.glob(f"/etc/NetworkManager/system-connections/{nom}.nmconnection")
    fichiers += [f for f in glob.glob("/etc/NetworkManager/system-connections/*.nmconnection")
                 if f not in fichiers]

    for chemin in fichiers:
        try:
            contenu = subprocess.run(["sudo", "-n", "cat", chemin],
                                     capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        if "[wifi]" not in contenu:
            continue
        ssid = re.search(r"^ssid=(.+)$", contenu, re.M)
        psk = re.search(r"^psk=(.+)$", contenu, re.M)
        if not ssid or not psk:
            continue
        valeur = psk.group(1).strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", valeur):
            raise SystemExit("mot de passe Wi-Fi stocke hache (64 hex) : non reversible, "
                             "il faut le fournir a la main avec ~/provisionner_wifi.sh")
        return ssid.group(1).strip(), valeur
    raise SystemExit("aucun profil Wi-Fi exploitable trouve sur cette machine")


def lire(serie, duree=1.5):
    fin = time.time() + duree
    tampon = b""
    while time.time() < fin:
        attente = serie.in_waiting
        if attente:
            tampon += serie.read(attente)
        else:
            time.sleep(0.05)
    return tampon.decode("utf-8", "replace")


def afficher(texte):
    for ligne in texte.splitlines():
        if any(mot in ligne for mot in AFFICHER_LIGNES):
            print("   " + ligne.strip())
            if "adresse IP" in ligne:
                trouve = re.search(r"adresse IP (\S+)", ligne)
                return trouve.group(1) if trouve else None
    return None


def main():
    import serial  # pyserial, fourni par le venv esptool

    port = trouver_port()
    if not port:
        raise SystemExit("aucune carte detectee (cable USB donnees ?)")
    print(f"  carte sur {port}")

    ssid, motdepasse = identifiants_systeme()
    print(f"  profil Wi-Fi lu sur cette machine : SSID de {len(ssid)} caracteres, "
          f"mot de passe de {len(motdepasse)} caracteres (jamais affiches)")
    if " " in motdepasse:
        raise SystemExit("le mot de passe contient un espace : la console du noeud ne sait pas "
                         "le distinguer des arguments, fournir une variante entre guillemets")

    with serial.Serial(port, 115200, timeout=1, dsrdtr=False, rtscts=False) as serie:
        serie.dtr = True
        serie.rts = False
        time.sleep(0.4)
        serie.reset_input_buffer()

        # Verification que le firmware repond AVANT d'ecrire : ecrire dans le vide donnerait un
        # faux succes (carte en mode telechargement par exemple).
        serie.write(b"INFO\n")
        retour = lire(serie, 1.5)
        if "etat=" not in retour and "etat:" not in retour:
            print("  pas de reponse a INFO : on tente un redemarrage par chien-de-garde")
            subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32c3", "-p", port,
                            "--before", "default_reset", "--after", "watchdog_reset", "read_mac"],
                           capture_output=True)
            time.sleep(3)
            serie.reset_input_buffer()
            serie.write(b"INFO\n")
            retour = lire(serie, 2.0)
            if "etat=" not in retour and "etat:" not in retour:
                print("  toujours muet. Dernier extrait lu (sans secret) :")
                print("  " + retour[-400:].replace("\n", "\n  "))
                raise SystemExit("le noeud ne repond pas")

        serie.reset_input_buffer()
        serie.write(f"WIFI {ssid} {motdepasse}\n".encode())
        retour = lire(serie, 6.0)

    ip = afficher(retour)
    print()
    if ip:
        print(f"  ✅ nœud provisionne et joignable sur {ip}")
        print(f"  (port TCP 3333 — nom DHCP attendu : s002-noeud)")
    else:
        print("  ⚠️ identifiants enregistres mais pas d'adresse IP vue : verifier le SSID, ou "
              "rebrancher la carte et relancer")


if __name__ == "__main__":
    main()
