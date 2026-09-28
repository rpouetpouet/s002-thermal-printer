#!/usr/bin/env python3
"""Installe et configure l'intégration S002 dans Home Assistant via l'API websocket.

Étapes indépendantes, appelables séparément :
  add       : déclare le dépôt custom dans HACS et localise son identifiant
  download  : télécharge la version demandée (par défaut v0.1.0)
  status    : état HACS du dépôt + présence de l'intégration (lecture seule)
  restart   : redémarre Home Assistant
  setup     : crée l'entrée de configuration de l'imprimante (config flow)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import websockets

ENV = pathlib.Path.home() / ".hermes" / ".env"
DEPOT = "rpouetpouet/s002-thermal-printer"
URL = f"https://github.com/{DEPOT}"
ADRESSE_DEFAUT = "06:03:DD:EC:16:4D"


def _env(cle: str) -> str:
    for ligne in ENV.read_text().splitlines():
        if ligne.startswith(f"{cle}="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absente de {ENV}")


class Client:
    def __init__(self, ws, ha_version: str) -> None:
        self.ws = ws
        self.ha_version = ha_version
        self._id = 0

    async def envoyer(self, **msg) -> dict:
        self._id += 1
        msg["id"] = self._id
        await self.ws.send(json.dumps(msg))
        while True:
            reponse = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=180))
            if reponse.get("id") == self._id:
                return reponse

    async def attendre_evenement(self, type_attendu: str, timeout: float = 60.0) -> dict | None:
        """Consomme les messages non sollicités (événements HACS) jusqu'au type voulu."""
        fin = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < fin:
            reste = fin - asyncio.get_event_loop().time()
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=reste))
            except asyncio.TimeoutError:
                return None
            if msg.get("type") == type_attendu:
                return msg
        return None


async def connecter():
    base = _env("HASS_URL").rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    ws = await websockets.connect(f"{base}/api/websocket", max_size=16 * 1024 * 1024)
    msg = json.loads(await ws.recv())
    await ws.send(json.dumps({"type": "auth", "access_token": _env("HASS_TOKEN")}))
    msg = json.loads(await ws.recv())
    if msg.get("type") != "auth_ok":
        sys.exit(f"authentification refusée : {msg}")
    return ws, Client(ws, msg.get("ha_version", "?"))


async def trouver_depot(client: Client) -> dict | None:
    reponse = await client.envoyer(type="hacs/repositories/list")
    if not reponse.get("success"):
        return None
    for depot in reponse["result"]:
        if (depot.get("full_name") or "").lower() == DEPOT.lower():
            return depot
    return None


async def cmd_add(_args) -> None:
    ws, client = await connecter()
    async with ws:
        print(f"Home Assistant {client.ha_version}")
        avant = await trouver_depot(client)
        if avant:
            print(f"dépôt déjà déclaré (id={avant['id']})")
        else:
            reponse = await client.envoyer(
                type="hacs/repositories/add", repository=URL, category="integration"
            )
            print("hacs/repositories/add ->", "OK" if reponse.get("success") else reponse.get("error"))
            await asyncio.sleep(4)
        depot = await trouver_depot(client)
        if not depot:
            sys.exit("ECHEC : dépôt introuvable après ajout")
        print(f"dépôt enregistré : id={depot['id']} | catégorie={depot.get('category')} | "
              f"installée={depot.get('installed_version') or '-'} | "
              f"dispo={depot.get('available_version') or '-'} | "
              f"dernière_version={depot.get('last_version') or '-'}")


async def cmd_download(args) -> None:
    ws, client = await connecter()
    async with ws:
        depot = await trouver_depot(client)
        if not depot:
            sys.exit("ECHEC : dépôt non déclaré (lancer 'add' d'abord)")
        print(f"téléchargement de {DEPOT} version {args.version} (id={depot['id']})…")
        reponse = await client.envoyer(
            type="hacs/repository/download", repository=depot["id"], version=args.version
        )
        if not reponse.get("success"):
            sys.exit(f"ECHEC téléchargement : {json.dumps(reponse.get('error'), ensure_ascii=False)}")
        print("téléchargement -> OK")
        await asyncio.sleep(3)
        depot = await trouver_depot(client)
        print(f"version installée = {depot.get('installed_version')} | "
              f"version disponible = {depot.get('available_version')}")


async def cmd_status(_args) -> None:
    ws, client = await connecter()
    async with ws:
        info = await client.envoyer(type="hacs/info")
        detail = info.get("result") or {}
        etat_hacs = (f"HACS {detail.get('version')} ({detail.get('stage')})"
                     if detail else
                     f"HACS non prêt : {json.dumps(info.get('error'), ensure_ascii=False)[:160]}")
        print(f"Home Assistant {client.ha_version} | {etat_hacs}")
        depot = await trouver_depot(client)
        if depot:
            print(f"{DEPOT} : installée={depot.get('installed_version') or '-'} "
                  f"dispo={depot.get('available_version') or '-'} "
                  f"catégorie={depot.get('category')}")
        else:
            print(f"{DEPOT} : absent de HACS")
        # les fichiers de l'intégration sont-ils connus de HA ? (preuve indépendante de HACS)
        reponse = await client.envoyer(type="manifest/get", integration="s002_printer")
        if reponse.get("success"):
            m = reponse["result"]
            print(f"manifeste chargé par HA : {m.get('name')} v{m.get('version')} | "
                  f"requirements={m.get('requirements')} | depends={m.get('dependencies')}")
        else:
            print(f"manifeste s002_printer ABSENT : "
                  f"{json.dumps(reponse.get('error'), ensure_ascii=False)[:200]}")
        # l'intégration est-elle déclarée côté HA ?
        reponse = await client.envoyer(type="config_entries/get")
        if reponse.get("success"):
            entrees = [e for e in reponse["result"] if e.get("domain") == "s002_printer"]
            print(f"entrées de configuration s002_printer : {len(entrees)}")
            for e in entrees:
                print(f"   {e.get('title')} | state={e.get('state')} | "
                      f"data={e.get('data')} | options={e.get('options')}")
            domaines = sorted({e.get("domain") for e in reponse["result"]})
            print(f"intégration chargée par HA : {'OUI' if 's002_printer' in domaines else 'NON'}")


async def cmd_restart(_args) -> None:
    ws, client = await connecter()
    print("envoi du redémarrage…")
    reponse = await client.envoyer(
        type="call_service", domain="homeassistant", service="restart"
    )
    print("homeassistant.restart ->", "OK" if reponse.get("success") else reponse.get("error"))
    await ws.close()


async def cmd_setup(args) -> None:
    """Crée l'entrée de configuration via le config flow (équivalent de l'assistant UI)."""
    ws, client = await connecter()
    async with ws:
        reponse = await client.envoyer(type="config_entries/flow/create", handler="s002_printer")
        if not reponse.get("success"):
            sys.exit(f"ECHEC création du flow : "
                     f"{json.dumps(reponse.get('error'), ensure_ascii=False)}")
        flow = reponse["result"]
        print(f"flow créé : {flow['flow_id']} | étape={flow.get('step_id')}")
        donnees = {
            "address": args.address,
            "name": args.name,
            "chunk_size": 200,
            "lines_per_frame": 40,
            "frame_pause_ms": 0,
            "feed_before_mm": 0.0,
            "feed_after_mm": 0.0,
        }
        reponse = await client.envoyer(
            type=f"config_entries/flow/{flow['flow_id']}", user_input=donnees
        )
        if not reponse.get("success"):
            sys.exit(f"ECHEC configuration : "
                     f"{json.dumps(reponse.get('error'), ensure_ascii=False)}")
        resultat = reponse["result"]
        print(f"résultat : type={resultat.get('type')} titre={resultat.get('title')}")
        if resultat.get("type") == "create_entry":
            print(f"ENTRÉE CRÉÉE : {resultat.get('title')}")


async def main() -> None:
    parseur = argparse.ArgumentParser(description=__doc__)
    sous = parseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("add")
    d = sous.add_parser("download"); d.add_argument("--version", default="v0.1.0")
    sous.add_parser("status")
    sous.add_parser("restart")
    s = sous.add_parser("setup")
    s.add_argument("--address", default=ADRESSE_DEFAUT)
    s.add_argument("--name", default="S002 Skynet")
    args = parseur.parse_args()
    await {"add": cmd_add, "download": cmd_download, "status": cmd_status,
           "restart": cmd_restart, "setup": cmd_setup}[args.commande](args)


if __name__ == "__main__":
    asyncio.run(main())
