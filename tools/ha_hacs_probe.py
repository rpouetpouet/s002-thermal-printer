#!/usr/bin/env python3
"""Sonde LECTURE SEULE de l'API websocket de Home Assistant (état de HACS).

Ne modifie rien : liste les dépôts HACS connus et l'état de l'intégration HACS.
Usage : ~/.hermes/hermes-agent/venv/bin/python3 ha_hacs_probe.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets

ENV = pathlib.Path.home() / ".hermes" / ".env"


def _env(cle: str) -> str:
    for ligne in ENV.read_text().splitlines():
        if ligne.startswith(f"{cle}="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absente de {ENV}")


async def main() -> None:
    base = _env("HASS_URL").rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/api/websocket"
    token = _env("HASS_TOKEN")
    print(f"Connexion à {url}")
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        msg = json.loads(await ws.recv())          # auth_required
        print("  ->", msg.get("type"))
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        msg = json.loads(await ws.recv())
        print("  ->", msg.get("type"), msg.get("message", ""))
        if msg.get("type") != "auth_ok":
            sys.exit(f"authentification refusée : {msg}")

        version = msg.get("ha_version")
        print(f"Home Assistant {version}")

        for i, requete in enumerate(
            ({"type": "hacs/info"}, {"type": "hacs/repositories/list"}), start=1
        ):
            await ws.send(json.dumps({"id": i, "type": requete["type"]}))
            reponse = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if not reponse.get("success"):
                print(f"\n{requete['type']} -> ECHEC : "
                      f"{json.dumps(reponse.get('error'), ensure_ascii=False)[:300]}")
                continue
            resultat = reponse["result"]
            if isinstance(resultat, dict):
                print(f"\n{requete['type']} ->")
                for cle in ("version", "ha_version", "enabled", "categories", "stage"):
                    if cle in resultat:
                        print(f"    {cle} = {resultat[cle]}")
            else:
                print(f"\n{requete['type']} -> {len(resultat)} dépôt(s)")
                for depot in resultat:
                    nom = depot.get("full_name") or depot.get("name")
                    print(f"    [{depot.get('category'):<12}] {nom:<52} "
                          f"v{depot.get('installed_version') or '-':<10} "
                          f"nouv={depot.get('available_version') or '-':<10} "
                          f"id={depot.get('id')}")

asyncio.run(main())
