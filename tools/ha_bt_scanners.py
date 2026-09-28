#!/usr/bin/env python3
"""Cartographie BLE depuis Home Assistant : quels scanners/proxys voient une adresse ?

Répond à une question concrète : « l'imprimante est-elle fixée à un proxy, ou puis-je la
déplacer ? » — l'intégration ne fixe rien : c'est habluetooth qui route chaque connexion
vers le meilleur scanner *connectable*. Ce script montre, chiffres en main :
  1. les scanners déclarés (nom, source, connectable) ;
  2. les connexions en cours par scanner (`subscribe_connection_allocations`) ;
  3. pendant N secondes, quels scanners entendent l'adresse visée et à quel RSSI.

⚠️ Les commandes `bluetooth/subscribe_*` envoient un `result` VIDE puis les données en
messages `event` (vérifié dans `components/bluetooth/websocket_api.py`) : il faut donc
lire les événements, pas le result (piège rencontré).

Usage :
  ~/.hermes/hermes-agent/venv/bin/python3 tools/ha_bt_scanners.py [MAC] [secondes]
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets

ENV = pathlib.Path.home() / ".hermes" / ".env"
MAC_DEFAUT = "06:03:DD:EC:16:4D"


def env(cle: str) -> str:
    for ligne in ENV.read_text().splitlines():
        if ligne.startswith(f"{cle}="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absent de {ENV}")


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0

    async def envoyer(self, **msg) -> None:
        self._id += 1
        msg["id"] = self._id
        self._prochain = self._id
        await self.ws.send(json.dumps(msg))

    async def _lire(self, timeout: float = 30.0) -> dict:
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))

    async def souscrire(self, duree: float, type_: str) -> tuple[list, list]:
        """Souscrit et collecte : rend (result, [événements]) pendant `duree` secondes."""
        await self.envoyer(type=type_)
        ident = self._prochain
        resultat: list = []
        evenements: list = []
        fin = asyncio.get_event_loop().time() + duree
        while True:
            reste = fin - asyncio.get_event_loop().time()
            if reste <= 0:
                break
            try:
                msg = await self._lire(reste)
            except asyncio.TimeoutError:
                break
            if msg.get("id") != ident:
                continue
            if msg.get("type") == "result":
                resultat = msg.get("result") or []
            elif msg.get("type") == "event":
                evenements.append(msg.get("event"))
        return resultat, evenements


async def main() -> None:
    mac = (sys.argv[1] if len(sys.argv) > 1 else MAC_DEFAUT).upper()
    duree = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    base = env("HASS_URL").rstrip("/").replace("https://", "wss://").replace("http://", "ws://")

    async with websockets.connect(f"{base}/api/websocket", max_size=32 * 1024 * 1024) as ws:
        json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "auth", "access_token": env("HASS_TOKEN")}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("authentification refusée")
        client = Client(ws)

        print("=== 1. Scanners déclarés dans Home Assistant ===")
        _, evenements = await client.souscrire(3.0, "bluetooth/subscribe_scanner_details")
        scanners: dict[str, dict] = {}
        for evenement in evenements:
            for details in evenement.get("add", []):
                scanners[details.get("source") or details.get("name") or "?"] = details
        if not scanners:
            print("  (aucun scanner remonté)")
        for source, details in sorted(scanners.items()):
            print(f"  {str(details.get('name')):<28} source={str(details.get('source')):<26} "
                  f"connectable={details.get('connectable')} type={details.get('type')}")

        print("\n=== 2. Connexions BLE en cours (par scanner) ===")
        resultat, evenements = await client.souscrire(3.0, "bluetooth/subscribe_connection_allocations")
        allocations: list = []
        for evenement in evenements:
            allocations = evenement if isinstance(evenement, list) else [evenement]
        if not allocations:
            print("  (aucune connexion active — une impression en cours apparaîtrait ici)")
        for allocation in allocations:
            print(f"  {allocation}")

        print(f"\n=== 3. Qui entend {mac} pendant {duree:.0f} s ? ===")
        await client.envoyer(type="bluetooth/subscribe_advertisements")
        ident = client._prochain
        vus: dict[str, dict] = {}
        fin = asyncio.get_event_loop().time() + duree
        while asyncio.get_event_loop().time() < fin:
            try:
                msg = await client._lire(max(0.1, fin - asyncio.get_event_loop().time()))
            except asyncio.TimeoutError:
                break
            if msg.get("id") != ident or msg.get("type") != "event":
                continue
            # Les annonces arrivent par LOTS : event = {"add": [device, ...]} (vérifié).
            for adv in (msg.get("event") or {}).get("add") or []:
                if (adv.get("address") or "").upper() != mac:
                    continue
                source = adv.get("source") or adv.get("scanner") or "?"
                info = vus.setdefault(source, {"n": 0, "rssi": []})
                info["n"] += 1
                if (rssi := adv.get("rssi")) is not None:
                    info["rssi"].append(rssi)

        if not vus:
            print("  aucun scanner n'a entendu cette adresse (éteinte, hors portée, ou filtrée)")
        for source, info in sorted(vus.items(), key=lambda kv: -kv[1]["n"]):
            rssi = info["rssi"]
            detail = (f"RSSI {min(rssi)}..{max(rssi)} dBm (moy {sum(rssi)/len(rssi):.0f})"
                      if rssi else "RSSI inconnu")
            print(f"  {source:<28} {info['n']:>4} annonces | {detail}")


asyncio.run(main())
