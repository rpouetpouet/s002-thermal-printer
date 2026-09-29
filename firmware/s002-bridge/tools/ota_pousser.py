#!/usr/bin/env python3
"""Met a jour le firmware du noeud S002 PAR LE RESEAU — sans cable, sans demonter l'imprimante.

Pourquoi cet outil existe : atteindre le port USB du C3 demande d'ouvrir le boitier de
l'imprimante. Le premier flash « compatible OTA » se fait donc par USB (c'est celui du
29/09/2026), et tous les suivants passent par le Wi-Fi avec ce script.

Le noeud ecrit l'image dans la partition INACTIVE (ota_0 <-> ota_1) et ne bascule dessus
qu'apres validation : une coupure au milieu du transfert laisse le firmware qui tourne intact.

    python tools/ota_pousser.py firmware/s002-bridge/build/s002-bridge.bin
    python tools/ota_pousser.py <binaire> --hote 192.168.42.62 --port 3333

Le port 3333 melange deux modes : le premier octet `0x64` annonce un flux de trames YK, tout
autre premier octet est une commande texte (PING, STATUS, LIBERER, OTA <octets>). Cet outil
utilise le mode texte.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

HOTE_DEFAUT = "192.168.42.62"
PORT_DEFAUT = 3333
TAILLE_MINIMALE = 100_000      # en dessous, ce n'est pas une image d'application valide
BLOC = 4096


def lire_reponse(sock: socket.socket, delai: float = 20.0) -> str:
    """Lit jusqu'a la premiere ligne terminee, ou jusqu'a expiration."""
    sock.settimeout(delai)
    tampon = b""
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        try:
            bloc = sock.recv(256)
        except socket.timeout:
            break
        if not bloc:
            break
        tampon += bloc
        if b"\n" in tampon:
            break
    return tampon.decode(errors="replace").strip()


def pousser(chemin: str, hote: str, port: int) -> int:
    taille = os.path.getsize(chemin)
    if taille < TAILLE_MINIMALE:
        print(f"  ECHEC : {chemin} ne fait que {taille} octets — ce n'est pas une image "
              f"d'application (au moins {TAILLE_MINIMALE} attendus)")
        return 2

    print(f"  image   : {chemin} ({taille} octets)")
    print(f"  noeud   : {hote}:{port}")
    with open(chemin, "rb") as f:
        image = f.read()

    with socket.create_connection((hote, port), timeout=10) as sock:
        # 1) etat AVANT : la partition qui tourne doit basculer apres la mise a jour
        sock.sendall(b"STATUS\n")
        avant = lire_reponse(sock)
        print(f"  avant   : {avant}")
        if "partition=" not in avant:
            print("  ATTENTION : ce firmware ne dit pas sur quelle partition il tourne ; la "
                  "bascule ne sera pas verifiable apres coup.")

        # 2) annonce de la taille, le noeud repond quand il est pret a recevoir
        sock.sendall(f"OTA {taille}\n".encode())
        pret = lire_reponse(sock)
        print(f"  noeud   : {pret}")
        if "OTA PRET" not in pret:
            print("  ECHEC : le noeud n'a pas reconnu la commande OTA (firmware anterieur a la "
                  "v2 ? la commande est alors rejetee comme inconnue)")
            return 3

        # 3) envoi du binaire
        debut = time.monotonic()
        envoyes = 0
        while envoyes < taille:
            sock.sendall(image[envoyes:envoyes + BLOC])
            envoyes += BLOC
            pourcent = 100 * min(envoyes, taille) / taille
            print(f"\r  envoi   : {pourcent:5.1f} % ({envoyes}/{taille} octets)", end="",
                  flush=True)
        duree = time.monotonic() - debut
        print(f"\r  envoi   : 100,0 % en {duree:.1f} s ({taille / duree / 1024:.0f} Ko/s)")

        # 4) verdict du noeud (l'image validee fait redemarrer le noeud, le socket tombe)
        verdict = lire_reponse(sock)
        print(f"  noeud   : {verdict or '(socket ferme par le redemarrage)'}")

    # 5) le noeud redemarre : attendre qu'il reponde, puis relire la partition
    print("  attente du redemarrage…")
    for _ in range(30):
        time.sleep(2)
        try:
            with socket.create_connection((hote, port), timeout=5) as sock:
                sock.sendall(b"STATUS\n")
                apres = lire_reponse(sock)
            if "etat=" in apres:
                print(f"  apres   : {apres}")
                if "partition=" in avant and "partition=" in apres:
                    p_avant = avant.split("partition=")[1].split()[0]
                    p_apres = apres.split("partition=")[1].split()[0]
                    if p_avant != p_apres:
                        print(f"  OK : mise a jour par le RESEAU confirmee ({p_avant} -> "
                              f"{p_apres})")
                        return 0
                    print(f"  ATTENTION : le noeud repond, mais tourne toujours sur {p_apres} "
                          "— la bascule a peut-etre echoue")
                    return 1
                return 0
        except OSError:
            continue
    print("  ECHEC : le noeud ne repond plus apres la mise a jour (cable USB necessaire)")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binaire", help="chemin du .bin d'application a envoyer")
    p.add_argument("--hote", default=HOTE_DEFAUT)
    p.add_argument("--port", type=int, default=PORT_DEFAUT)
    a = p.parse_args()
    return pousser(a.binaire, a.hote, a.port)


if __name__ == "__main__":
    sys.exit(main())
