"""Transport BLE de l'ORGSTA S002 via la pile Bluetooth de Home Assistant.

Ce module ne parle PAS directement à l'adaptateur : il passe par
`homeassistant.components.bluetooth`, ce qui permet à HA de router la connexion
GATT **à travers un proxy BLE ESP32/RP2** (`bluetooth_proxy: active: true`) ou par
un adaptateur local, sans changer une ligne de code.

Deux particularités MESURÉES sur le matériel :
  * l'imprimante expose le service `ff00` en DEUX exemplaires (appareil multi-link) ;
    l'écriture utile est la SECONDE occurrence (handle le plus haut, `service000c`
    sous BlueZ). Écrire sur la première ne produit rien : on choisit donc la
    caractéristique PAR HANDLE, pas par UUID ;
  * le flux est régulé par des crédits envoyés sur `ff03` (`01 05` = 5 paquets).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import yk
from .const import (
    DEFAULT_CHUNK_SIZE,
    FLOW_TIMEOUT_MS,
    FLOW_WINDOW,
    NOTIFY_FLOW_UUID,
    NOTIFY_STATE_UUID,
    SERVICE_UUID,
    WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)


class S002Error(Exception):
    """Erreur de communication avec l'imprimante."""


@dataclass
class WriteStats:
    """Mesures de débit — c'est ce qui décide si le passage par proxy tient la charge."""

    connect_ms: float = 0.0
    frames: int = 0
    chunks: int = 0
    bytes_written: int = 0
    frame_ms: list[float] = field(default_factory=list)
    flow_waits: int = 0
    flow_timeout: int = 0
    notify_state: int = 0
    notify_flow: int = 0
    notify_errors: list[str] = field(default_factory=list)
    channel_handle: int | None = None
    channels_seen: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        total = sum(self.frame_ms)
        return {
            "connect_ms": round(self.connect_ms, 1),
            "frames": self.frames,
            "chunks": self.chunks,
            "bytes_written": self.bytes_written,
            "total_frame_ms": round(total, 1),
            "avg_frame_ms": round(total / self.frames, 1) if self.frames else 0.0,
            "max_frame_ms": round(max(self.frame_ms), 1) if self.frame_ms else 0.0,
            "throughput_kbps": round(self.bytes_written / total, 2) if total else 0.0,
            "flow_waits": self.flow_waits,
            "flow_timeout": self.flow_timeout,
            "notify_state": self.notify_state,
            "notify_flow": self.notify_flow,
            "notify_errors": self.notify_errors,
            "channel_handle": self.channel_handle,
            "channels_seen": self.channels_seen,
        }


class S002Transport:
    """Connexion BLE, écriture des trames et mesure du débit."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str = "S002",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self.name = name
        # int() : les sélecteurs numériques de HA renvoient des flottants (cf. yk.chunks).
        self.chunk_size = max(20, min(237, int(chunk_size)))  # MTU 240 → 237 utiles au max
        self.stats = WriteStats()
        self._client: BleakClientWithServiceCache | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._credits = 0
        self._credit_event = asyncio.Event()

    # ------------------------------------------------------------------ connexion
    async def connect(self) -> None:
        """Établit la connexion (routée par HA : proxy ou adaptateur local)."""
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise S002Error(
                f"Imprimante {self.address} introuvable : elle doit être à portée d'un "
                "adaptateur ou d'un proxy BLE déclaré comme 'connectable' "
                "(bluetooth_proxy: active: true, ESP32/RP2040 uniquement)"
            )
        _LOGGER.debug("Connexion à %s via %s", self.address, getattr(device, "name", "?"))
        debut = time.monotonic()
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.name,
            max_attempts=3,
            use_services_cache=False,
        )
        self.stats.connect_ms = (time.monotonic() - debut) * 1000
        _LOGGER.info(
            "S002 %s : connecté en %.0f ms", self.address, self.stats.connect_ms
        )
        self._select_channel()
        await self._subscribe_notifications()

    def _select_channel(self) -> None:
        """Choisit la caractéristique d'écriture : la SECONDE occurrence du service ff00."""
        assert self._client is not None
        candidats: list[BleakGATTCharacteristic] = []
        for service in self._client.services:
            if service.uuid.lower() != SERVICE_UUID:
                continue
            for char in service.characteristics:
                if char.uuid.lower() == WRITE_UUID:
                    candidats.append(char)
        self.stats.channels_seen = [
            f"handle=0x{c.handle:04x} service={c.service_uuid}" for c in candidats
        ]
        if not candidats:
            raise S002Error(
                f"Caractéristique d'écriture {WRITE_UUID} absente : "
                f"services vus = {[s.uuid for s in self._client.services]}"
            )
        candidats.sort(key=lambda c: c.handle)
        # La seconde occurrence (handle le plus haut) est le canal utile.
        self._write_char = candidats[-1]
        self.stats.channel_handle = self._write_char.handle
        _LOGGER.info(
            "S002 : %d canal(aux) d'écriture trouvé(s) %s → utilisation du handle 0x%04x",
            len(candidats),
            self.stats.channels_seen,
            self._write_char.handle,
        )

    async def _subscribe_notifications(self) -> None:
        """S'abonne à l'état (ff01) et au contrôle de flux (ff03)."""
        assert self._client is not None

        def _on_flow(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            self.stats.notify_flow += 1
            if len(data) >= 1 and data[0] == 0x01:
                self._credits += 1
                self._credit_event.set()

        def _on_state(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            self.stats.notify_state += 1
            if len(data) >= 13 and data[0] == 0x64:
                # payload = data[5:13] ; data[6] passe à 0x0b quand l'imprimante exécute
                _LOGGER.debug("S002 état : %s", bytes(data).hex(" "))

        for uuid, callback in ((NOTIFY_FLOW_UUID, _on_flow), (NOTIFY_STATE_UUID, _on_state)):
            try:
                await self._client.start_notify(uuid, callback)
            except Exception as err:  # noqa: BLE001 - l'absence de notify ne doit pas bloquer
                self.stats.notify_errors.append(f"{uuid}: {type(err).__name__}: {err}")
                _LOGGER.warning("S002 : abonnement %s impossible (%s)", uuid, err)

    # ------------------------------------------------------------------ écriture
    async def _write_chunk(self, morceau: bytes) -> None:
        assert self._client is not None and self._write_char is not None
        await self._client.write_gatt_char(self._write_char, morceau, response=False)
        self.stats.chunks += 1
        self.stats.bytes_written += len(morceau)

    async def write_frame(self, trame: bytes, chunk_size: int | None = None) -> float:
        """Écrit une trame complète, en respectant le contrôle de flux. Rend sa durée (ms)."""
        taille = chunk_size or self.chunk_size
        morceaux = yk.chunks(trame, taille)
        debut = time.monotonic()
        for i, morceau in enumerate(morceaux, start=1):
            await self._write_chunk(morceau)
            # Fenêtre de flux : on attend un crédit tous les 5 paquets (sauf au dernier).
            if i % FLOW_WINDOW == 0 and i < len(morceaux):
                self._credit_event.clear()
                avant = self._credits
                try:
                    await asyncio.wait_for(
                        self._attendre_credit(avant), timeout=FLOW_TIMEOUT_MS / 1000
                    )
                    self.stats.flow_waits += 1
                except asyncio.TimeoutError:
                    self.stats.flow_timeout += 1
                    _LOGGER.debug("S002 : pas de crédit de flux après %d paquets", i)
        duree = (time.monotonic() - debut) * 1000
        self.stats.frames += 1
        self.stats.frame_ms.append(duree)
        return duree

    async def _attendre_credit(self, avant: int) -> None:
        while self._credits == avant:
            await self._credit_event.wait()
            self._credit_event.clear()

    # ------------------------------------------------------------------ arrêt
    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("S002 : déconnexion non propre (%s)", err)
            finally:
                self._client = None

    async def __aenter__(self) -> "S002Transport":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()
