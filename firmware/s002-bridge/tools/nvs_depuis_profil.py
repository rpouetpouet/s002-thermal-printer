#!/usr/bin/env python3
"""Construit une image de partition NVS contenant les identifiants Wi-Fi.

Le profil Wi-Fi (fichier `.nmconnection` d'une autre machine) arrive sur l'ENTREE STANDARD :
il n'est jamais ecrit sur le disque en clair plus longtemps que necessaire, et surtout il
n'apparait ni dans un argument de commande, ni dans une sortie, ni dans un journal.

Sortie : l'image binaire NVS (a flasher sur la partition nvs) + un compte rendu SANS secret.
"""

import csv
import os
import re
import subprocess
import sys

GENERATEUR = "-m esp_idf_nvs_partition_gen"
SORTIE = "/home/batman/.hermes/cache/scratch/nvs_wifi.bin"
CSV = "/home/batman/.hermes/cache/scratch/nvs_wifi.csv"
TAILLE_PARTITION = "0x6000"  # doit correspondre a partitions.csv (nvs @0x9000, 24 Ko)


def main():
    profil = sys.stdin.read()
    ssid = re.search(r"^ssid=(.+)$", profil, re.M)
    psk = re.search(r"^psk=(.+)$", profil, re.M)
    if not ssid or not psk:
        raise SystemExit("profil illisible : ssid ou psk absent")
    ssid, motdepasse = ssid.group(1).strip(), psk.group(1).strip()

    if re.fullmatch(r"[0-9a-fA-F]{64}", motdepasse):
        raise SystemExit("mot de passe hache (64 hex) : non reversible")
    if "," in ssid or "," in motdepasse or '"' in ssid or '"' in motdepasse:
        raise SystemExit("virgule ou guillemet dans les identifiants : forme non geree")

    # Le CSV contient le secret : ecriture en 0600 puis suppression immediate.
    with open(CSV, "w", newline="") as f:
        f.write("key,type,encoding,value\n")
        f.write("s002,namespace,,\n")
        f.write(f"ssid,data,string,{ssid}\n")
        f.write(f"pass,data,string,{motdepasse}\n")
    os.chmod(CSV, 0o600)

    try:
        r = subprocess.run([sys.executable] + GENERATEUR.split() +
                           ["generate", CSV, SORTIE, TAILLE_PARTITION],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  ERREUR generateur :", (r.stdout + r.stderr).strip()[-300:])
            raise SystemExit(1)
    finally:
        os.remove(CSV)  # le CSV en clair ne survit pas a la generation

    print(f"  identifiants lus : SSID de {len(ssid)} caracteres, "
          f"mot de passe de {len(motdepasse)} caracteres (jamais affiches)")
    print(f"  image NVS : {os.path.getsize(SORTIE)} octets ({TAILLE_PARTITION})")


if __name__ == "__main__":
    main()
