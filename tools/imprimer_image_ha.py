#!/usr/bin/env python3
"""Imprime un fichier image local via le service `print_image` de Home Assistant.

Passe par l'intégration, donc par les réglages publiés (préparation + gamma 0.85 + tramage), et
vérifie la cohérence entre ce que HA annonce et ce que le nœud a réellement écrit sur le papier.

Usage : imprimer_image_ha.py <fichier> [--grand]
        --grand : imprime la photo tournée d'un quart de tour (1,8x plus de points par pixel)
"""
from __future__ import annotations

import base64
import io
import json
import pathlib
import socket
import sys
import time
import urllib.request

FICHIER = pathlib.Path(sys.argv[1])
GRAND = "--grand" in sys.argv


def env(cle: str) -> str:
    for ligne in pathlib.Path("/home/batman/.hermes/.env").read_text().splitlines():
        if ligne.startswith(cle + "="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absent")


def noeud(essais: int = 5) -> dict:
    """Lit le STATUS du nœud en s'assurant d'avoir la réponse COMPLÈTE.

    ⚠️ Piège vécu : un `recv()` unique peut renvoyer la réponse tronquée. Un `uptime=528s` coupé
    après `uptime=52` fait croire à un redémarrage qui n'a pas eu lieu — et c'est exactement le genre
    de faux positif qui envoie chercher une panne d'alimentation imaginaire. On boucle donc jusqu'à
    voir le dernier champ (`uptime=`), et on recommence sinon.
    """
    for _ in range(essais):
        try:
            with socket.create_connection(("192.168.42.62", 3333), timeout=10) as s:
                s.sendall(b"STATUS\n")
                s.settimeout(6)
                morceaux = []
                while True:
                    try:
                        bout = s.recv(4096)
                    except socket.timeout:
                        break
                    if not bout:
                        break
                    morceaux.append(bout.decode(errors="replace"))
                    if "uptime=" in "".join(morceaux):
                        break
            brut = "".join(morceaux).strip()
            if "etat=" in brut and "uptime=" in brut:
                return {m.split("=", 1)[0]: m.split("=", 1)[1] for m in brut.split() if "=" in m}
        except OSError:
            pass
        time.sleep(3)
    return {}


donnees = FICHIER.read_bytes()
if GRAND:
    from PIL import Image
    with Image.open(io.BytesIO(donnees)) as img:
        # La hauteur occupe la largeur de la tête, puis on tourne : la largeur de l'image s'étale sur
        # la longueur du rouleau. 0,80 point par pixel au lieu de 0,45.
        hauteur = 576
        largeur = round(img.width * hauteur / img.height)
        # `Image.LANCZOS` reste un alias déprécié : on passe par l'énumération quand elle existe.
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        tournee = img.resize((largeur, hauteur), lanczos).rotate(90, expand=True)
        tampon = io.BytesIO()
        tournee.save(tampon, format="PNG")
    donnees = tampon.getvalue()
    print(f"  mode GRAND : image tournée, {largeur}x{hauteur} px -> bande de "
          f"{hauteur / 11.81:.1f} x {largeur / 11.81:.1f} mm")

avant = noeud()
print(f"  AVANT : liaison={avant.get('liaison')} batterie={avant.get('batterie')} "
      f"uptime={avant.get('uptime')} ecritures={avant.get('ecritures')} reset={avant.get('reset')}")

requete = urllib.request.Request(
    env("HASS_URL") + "/api/services/s002_printer/print_image?return_response=true",
    data=json.dumps({"image": base64.b64encode(donnees).decode()}).encode(),
    headers={"Authorization": "Bearer " + env("HASS_TOKEN"), "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(requete, timeout=300) as reponse:
    r = json.load(reponse).get("service_response", {})
print(f"  rapport HA : {r.get('raster_lines')} lignes, {r.get('height_mm')} mm, "
      f"{r.get('frames')} trames, {r.get('duration_s')} s, erreurs={r.get('errors')}")

time.sleep(6)
apres = noeud()
ecart = (int(apres.get("ecritures", 0)) - int(avant.get("ecritures", 0))) if apres else None
morceaux = r.get("timings", {}).get("chunks")
up = str(apres.get("uptime", "")).rstrip("s")
print(f"  APRES : batterie={apres.get('batterie')} uptime={apres.get('uptime')} "
      f"ecritures={apres.get('ecritures')} (écart {ecart} vs {morceaux} annoncés)")
print(f"  transport : {'cohérent' if ecart == morceaux else 'écart à expliquer'}"
      f" | alimentation : "
      + ("pas de redémarrage" if up.isdigit() and int(up) > 120 else f"REDÉMARRAGE ({apres.get('reset')})"))
