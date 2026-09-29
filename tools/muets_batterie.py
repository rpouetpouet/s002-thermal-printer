#!/usr/bin/env python3
"""Liste imprimée des capteurs batterie muets — source : l'historique (base recorder).

Pourquoi l'historique et pas l'API : juste après un redémarrage de Home Assistant, `last_updated` des
entités restaurées vaut l'heure du redémarrage (mesuré : 65/65 capteurs à « 0 min »). L'historique,
lui, survit aux redémarrages.

⚠️ Ce que l'historique contient, et ce qu'il ne contient pas : la base n'enregistre un état que quand
il **change**. L'âge affiché est donc celui du dernier CHANGEMENT, pas de la dernière émission. Un
capteur dont la valeur est stable depuis longtemps paraîtra muet à tort — d'où deux signaux plus
fiables imprimés en tête de liste : les entités `unavailable`/`unknown` (l'appareil ne répond plus du
tout) et les états restaurés après redémarrage.

Usage : muets_batterie.py [seuil_jours] [--print]
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import websockets

# On charge yk.py DIRECTEMENT, sans passer par le paquet : importer le paquet déclenche
# __init__.py, qui a besoin des dépendances de Home Assistant (voluptuous). Or le seul venv qui a
# `websockets` (nécessaire à l'API websocket) ne les a pas. Charger le fichier contourne le problème
# sans installer quoi que ce soit.
import importlib.util  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

# yk.py fait `from .const import …`, donc il exige un paquet. On reconstitue un paquet minimal dans
# un dossier temporaire (const.py + yk.py se suffisent à eux-mêmes, seul __init__.py du vrai paquet
# traîne des dépendances Home Assistant). Même méthode que tests/test_yk.py.
_paquet = pathlib.Path(tempfile.mkdtemp()) / "s002_printer"
_paquet.mkdir()
(_paquet / "__init__.py").write_text("")
for _nom in ("const.py", "yk.py"):
    shutil.copy(f"/home/batman/dev/ha-s002/custom_components/s002_printer/{_nom}", _paquet / _nom)
sys.path.insert(0, str(_paquet.parent))
from s002_printer import yk  # noqa: E402

SEUIL_J = float(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else 7.0
IMPRIMER = "--print" in sys.argv
MAX_LIGNES = 34


def env(cle: str) -> str:
    for ligne in pathlib.Path("/home/batman/.hermes/.env").read_text().splitlines():
        if ligne.startswith(cle + "="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absent")


HASS = env("HASS_URL")
TOKEN = env("HASS_TOKEN")
WS = HASS.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


def duree(delta: timedelta) -> str:
    if delta.days >= 1:
        return f"{delta.days} j"
    return f"{delta.seconds // 3600} h"


async def collecter() -> tuple[list[dict], list[dict]]:
    async with websockets.connect(WS, max_size=None, open_timeout=30) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise SystemExit("authentification websocket refusée")

        await ws.send(json.dumps({"id": 1, "type": "get_states"}))
        tous = json.loads(await ws.recv())["result"]

        batteries = []
        for entite in tous:
            attrs = entite.get("attributes", {})
            if attrs.get("device_class") == "battery" or (
                attrs.get("unit_of_measurement") == "%" and "batt" in entite["entity_id"].lower()
            ):
                batteries.append(entite)

        # Historique : on demande une fenêtre large pour mesurer l'âge du dernier changement.
        debut = (datetime.now(timezone.utc) - timedelta(days=max(60.0, SEUIL_J + 10))).isoformat()
        await ws.send(json.dumps({
            "id": 2, "type": "history/history_during_period",
            "start_time": debut, "entity_ids": [e["entity_id"] for e in batteries],
            "minimal_response": True, "no_attributes": True, "significant_changes_only": False,
        }))
        reponse = json.loads(await ws.recv())
        if reponse.get("success") is False:
            raise SystemExit(f"historique refusé : {reponse.get('error')}")
        return batteries, reponse["result"]

    return batteries, {}


maintenant_ts = datetime.now(timezone.utc).timestamp()
batteries, historique = asyncio.run(collecter())
print(f"  capteurs batterie : {len(batteries)} | entités avec historique : {len(historique)}")

lignes = []
for entite in batteries:
    eid = entite["entity_id"]
    entrees = historique.get(eid) or []
    if entrees:
        # minimal_response : dernier élément = {s: état, lu: epoch du dernier changement}
        dernier = entrees[-1]
        horodatage = dernier.get("lu") or dernier.get("lc") or 0
        age = timedelta(seconds=max(0.0, maintenant_ts - horodatage))
    else:
        age = timedelta(days=999)
    nom = entite["attributes"].get("friendly_name") or eid
    # Beaucoup de capteurs n'ont qu'un libellé générique (« batt ») : sur le papier, ça n'identifie
    # rien. Dans ce cas on prend l'identifiant, qui lui est unique.
    if len(nom) < 13 or nom.strip().lower() in ("batt", "battery", "batterie", "battery level"):
        nom = eid.replace("sensor.", "").replace("binary_sensor.", "")
    hors_ligne = entite["state"] in ("unavailable", "unknown")
    lignes.append({
        "nom": nom, "eid": eid, "age": age, "etat": entite["state"],
        "hors_ligne": hors_ligne,
    })

lignes.sort(key=lambda l: (not l["hors_ligne"], -l["age"].total_seconds()))
hors_ligne = [l for l in lignes if l["hors_ligne"]]
muets = [l for l in lignes if not l["hors_ligne"] and l["age"] > timedelta(days=SEUIL_J)]

print(f"  hors ligne : {len(hors_ligne)} | sans changement depuis plus de {SEUIL_J:.0f} j : {len(muets)}")
retenues = hors_ligne + muets
for l in retenues[:MAX_LIGNES]:
    marque = ("HORS LIGNE " + duree(l["age"])) if l["hors_ligne"] else duree(l["age"])
    print(f"    {marque:<15} {str(l['etat']):>7}  {l['nom']}")

# ---------------------------------------------------------------- impression
if not IMPRIMER:
    print("\n  (mode lecture seule — ajouter --print pour imprimer)")
    raise SystemExit(0)

if not retenues:
    print("  rien à imprimer : aucun capteur muet")
    raise SystemExit(0)

entete = yk.text_raster([
    "CAPTEURS BATTERIE MUETS",
    f"{datetime.now():%d/%m %H:%M} | seuil {SEUIL_J:.0f} j | {len(batteries)} capteurs",
    f"{len(hors_ligne)} hors ligne",
], scale=2)
corps = b""
for l in retenues[:MAX_LIGNES]:
    marque = "HORS LIGNE " + duree(l["age"]) if l["hors_ligne"] else duree(l["age"])
    corps += yk.text_raster([f"{l['nom'][:19]} | {marque} | {l['etat']}"], scale=2)
pied = b""
if len(retenues) > MAX_LIGNES:
    pied = yk.text_raster([f"... et {len(retenues) - MAX_LIGNES} autres"], scale=2)
pied += yk.text_raster([
    "Age = dernier CHANGEMENT enregistre.",
    "Une batterie stable n'est pas enregistree :",
    "verifier les appareils hors ligne en premier.",
], scale=2)

raster = entete + b"\x00" * yk.BYTES_PER_LINE * 10 + corps + b"\x00" * yk.BYTES_PER_LINE * 10 + pied \
         + b"\x00" * yk.BYTES_PER_LINE * 60      # marge pour arracher la bande proprement

total = len(raster) // yk.BYTES_PER_LINE
encre = 100.0 * sum(bin(b).count("1") for b in raster) / (len(raster) * 8)
print(f"\n  bande : {total} lignes = {total / yk.DOTS_PER_MM:.1f} mm, encre {encre:.1f} %, "
      f"{len(raster)//1024} Ko")


def noeud(essais: int = 5) -> dict:
    for _ in range(essais):
        try:
            with socket.create_connection(("192.168.42.62", 3333), timeout=10) as s:
                s.sendall(b"STATUS\n")
                s.settimeout(8)
                brut = s.recv(4096).decode().strip()
            if "etat=" in brut:
                return {m.split("=", 1)[0]: m.split("=", 1)[1] for m in brut.split() if "=" in m}
        except (OSError, socket.timeout):
            time.sleep(3)
    return {}


avant = noeud()
print(f"  AVANT : liaison={avant.get('liaison')} batterie={avant.get('batterie')} "
      f"uptime={avant.get('uptime')} ecritures={avant.get('ecritures')}")
requete = urllib.request.Request(
    HASS + "/api/services/s002_printer/print_raw?return_response=true",
    data=json.dumps({"data": base64.b64encode(raster).decode()}).encode(),
    headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(requete, timeout=300) as reponse:
    r = json.load(reponse).get("service_response", {})
print(f"  rapport HA : {r.get('raster_lines')} lignes, {r.get('height_mm')} mm, "
      f"{r.get('frames')} trames, {r.get('duration_s')} s, erreurs={r.get('errors')}")
time.sleep(6)
apres = noeud()
ecart = (int(apres.get("ecritures", 0)) - int(avant.get("ecritures", 0))) if apres else None
up = str(apres.get("uptime", "")).rstrip("s")
print(f"  APRES : batterie={apres.get('batterie')} uptime={apres.get('uptime')} "
      f"ecritures={apres.get('ecritures')} (écart {ecart} vs {r.get('timings', {}).get('chunks')})")
print("  => " + ("pas de redémarrage" if up.isdigit() and int(up) > 120 else
                 f"REDÉMARRAGE ({apres.get('reset')})"))
