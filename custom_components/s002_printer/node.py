"""Transport réseau : le nœud d'impression (firmware v1) exposé en TCP sur le réseau local.

Pourquoi ce transport : le passage par le proxy Bluetooth d'ESPHome coûtait 116 ms par paquet,
ce qui faisait dépasser à l'imprimante sa tolérance de pause (400 ms) et produisait des blancs.
Un ESP32-C3 dédié, posé à côté de l'imprimante et joignable en TCP, coûte ~14 ms par paquet.

Le partage des rôles est volontairement le même que pour le BLE : **l'ESP32 reste bête**.
C'est Home Assistant qui construit les trames YK (rasterisation comprise) ; le nœud se contente
de les écrire en Bluetooth avec réponse, en respectant les crédits de flux annoncés par
l'imprimante. Rien à rasteriser côté microcontrôleur, donc rien à maintenir en double.

⚠️ Différence importante avec le transport BLE : ici, c'est le NŒUD qui gère le contrôle de flux.
Le budget de trame adaptatif de `printer.py` (qui existait pour rester sous les 400 ms à travers
le proxy) n'a plus d'objet : ses mesures ne portent plus sur le lien radio. Utiliser des trames
longues (`lines_per_frame = 40`).
"""

from __future__ import annotations

import asyncio
import time

from .ble import S002Error, WriteStats

DEFAULT_NODE_PORT = 3333
# Délais : la connexion TCP est locale (millisecondes) ; une impression longue peut demander
# plusieurs dizaines de secondes, donc pas de timeout agressif côté écriture.
NODE_CONNECT_TIMEOUT_S = 5.0
NODE_REPONSE_TIMEOUT_S = 5.0


class S002NodeTransport:
    """Transport TCP vers le nœud d'impression.

    API strictement identique à `S002Transport` (ble.py) : `printer.py` peut passer de l'un à
    l'autre sans le savoir. Seuls `ping()` et `statut()` sont propres à ce transport.
    """

    def __init__(self, hote: str, port: int = DEFAULT_NODE_PORT) -> None:
        self.hote = hote
        self.port = int(port)
        self.stats = WriteStats()
        self._lecteur: asyncio.StreamReader | None = None
        self._ecrivain: asyncio.StreamWriter | None = None
        self._debut_connexion = 0.0

    # ------------------------------------------------------------------ cycle de vie
    async def connect(self) -> None:
        """Ouvre la connexion TCP vers le nœud."""
        debut = time.monotonic()
        try:
            self._lecteur, self._ecrivain = await asyncio.wait_for(
                asyncio.open_connection(self.hote, self.port), timeout=NODE_CONNECT_TIMEOUT_S
            )
        except asyncio.TimeoutError as err:
            raise S002Error(
                f"nœud {self.hote}:{self.port} : pas de réponse en {NODE_CONNECT_TIMEOUT_S} s "
                "(nœud éteint, hors Wi-Fi, ou port bloqué ?)"
            ) from err
        except OSError as err:
            raise S002Error(f"nœud {self.hote}:{self.port} injoignable — {err}") from err
        self._debut_connexion = time.monotonic()
        self.stats.connect_ms = (self._debut_connexion - debut) * 1000
        self.stats.channel_handle = None
        self.stats.scanner_source = f"nœud TCP {self.hote}:{self.port}"

    async def disconnect(self) -> None:
        """Ferme la connexion. Ne lève jamais : appelé depuis un `finally`."""
        if self._ecrivain is None:
            return
        try:
            self._ecrivain.close()
            await self._ecrivain.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            self._lecteur = None
            self._ecrivain = None

    async def __aenter__(self) -> "S002NodeTransport":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ écriture
    async def write_frame(self, trame: bytes, chunk_size: int | None = None) -> float:
        """Envoie une trame complète (octets YK bruts) et renvoie sa durée d'envoi en ms.

        `chunk_size` est accepté pour rester compatible avec l'appelant BLE, mais il n'a pas de
        sens ici : TCP segmente lui-même, et c'est le nœud qui découpe en paquets BLE de 200 o.
        """
        del chunk_size
        if not trame:
            raise S002Error("trame vide")
        if self._ecrivain is None:
            raise S002Error("transport non connecté : appeler connect() d'abord")

        debut = time.monotonic()
        try:
            self._ecrivain.write(trame)
            await self._ecrivain.drain()
        except (OSError, ConnectionError) as err:
            raise S002Error(f"écriture refusée par le nœud — {err}") from err
        duree = (time.monotonic() - debut) * 1000

        self.stats.frames += 1
        self.stats.bytes_written += len(trame)
        self.stats.frame_ms.append(duree)
        # Le nœud découpe en paquets de 200 o : on répartit la durée mesurée pour que les
        # champs de statistiques gardent un ordre de grandeur exploitable (et comparable aux
        # relevés BLE), sans prétendre mesurer ce qui se passe vraiment sur l'air.
        paquets = max(1, (len(trame) + 199) // 200)
        self.stats.chunks += paquets
        self.stats.chunk_ms.extend([duree / paquets] * paquets)
        self.stats.idle_ms.append(duree)
        return duree

    # ------------------------------------------------------------------ commandes texte
    async def _commande(self, commande: str) -> str:
        """Envoie une commande texte et lit une ligne en réponse."""
        if self._ecrivain is None or self._lecteur is None:
            raise S002Error("transport non connecté : appeler connect() d'abord")
        try:
            self._ecrivain.write((commande + "\n").encode("ascii"))
            await self._ecrivain.drain()
            ligne = await asyncio.wait_for(
                self._lecteur.readline(), timeout=NODE_REPONSE_TIMEOUT_S
            )
        except asyncio.TimeoutError as err:
            raise S002Error(f"nœud {self.hote} : pas de réponse à {commande}") from err
        except OSError as err:
            raise S002Error(f"nœud {self.hote} : échange interrompu — {err}") from err
        if not ligne:
            raise S002Error(f"nœud {self.hote} : connexion fermée pendant {commande}")
        return ligne.decode("utf-8", "replace").strip()

    async def ping(self) -> bool:
        """Vrai si le nœud répond. Sert de test de disponibilité, sans rien imprimer."""
        return (await self._commande("PING")).upper() == "PONG"

    async def statut(self) -> dict[str, object]:
        """État du nœud, tel qu'il se décrit lui-même (`cle=valeur` séparés par des espaces).

        ⚠️ Le nœud renvoie ce qu'il SAIT (liaison Bluetooth, crédits, paquets écrits). Ce n'est
        pas une preuve d'impression : seule la trame d'état de l'imprimante l'est.
        """
        brut = await self._commande("STATUS")
        champs: dict[str, object] = {}
        for morceau in brut.split():
            if "=" not in morceau:
                continue
            cle, valeur = morceau.split("=", 1)
            try:
                champs[cle] = int(valeur)
            except ValueError:
                champs[cle] = valeur
        if not champs:
            raise S002Error(f"réponse de STATUT illisible : {brut!r}")
        champs["brut"] = brut
        return champs
