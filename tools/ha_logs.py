#!/usr/bin/env python3
"""Lit le journal système récent de Home Assistant (lecture seule) via websocket."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets

ENV = pathlib.Path.home() / ".hermes" / ".env"


def env(cle: str) -> str:
    for ligne in ENV.read_text().splitlines():
        if ligne.startswith(f"{cle}="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absent")


async def main() -> None:
    filtre = sys.argv[1] if len(sys.argv) > 1 else "s002"
    base = env("HASS_URL").rstrip("/").replace("https://", "wss://")
    async with websockets.connect(f"{base}/api/websocket", max_size=16 * 1024 * 1024) as ws:
        json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "auth", "access_token": env("HASS_TOKEN")}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth refusée")
        await ws.send(json.dumps({"id": 1, "type": "system_log/list"}))
        reponse = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if not reponse.get("success"):
            sys.exit(f"system_log/list : {reponse.get('error')}")
        entrees = reponse["result"]
        print(f"{len(entrees)} entrée(s) de journal récentes")
        affichees = 0
        for entree in entrees:
            texte = json.dumps(entree, ensure_ascii=False)
            if filtre and filtre.lower() not in texte.lower():
                continue
            affichees += 1
            print("-" * 70)
            print(f"[{entree.get('level')}] {entree.get('name')} : {entree.get('message')}")
            print(f"  source : {json.dumps(entree.get('source'), ensure_ascii=False)[:300]}")
            exception = entree.get("exception")
            if exception:
                print(f"  exception : {exception[:4000]}")
            if affichees >= 8:
                break
        if affichees == 0:
            print(f"(aucune entrée contenant « {filtre} »)")


asyncio.run(main())
