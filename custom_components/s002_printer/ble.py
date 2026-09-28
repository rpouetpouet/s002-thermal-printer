"""Transport BLE de l'ORGSTA S002 via la pile Bluetooth de Home Assistant.

Ce module ne parle PAS directement à l'adaptateur : il passe par
`homeassistant.components.bluetooth`, ce qui permet à HA de router la connexion
GATT **à travers un proxy BLE ESP32/RP2040** (`bluetooth_proxy: active: true`) ou par
un adaptateur local, sans changer une ligne de code.

Particularités MESURÉES sur le matériel :
  * l'imprimante expose le service `ff00` en DEUX exemplaires (appareil multi-link) ;
    l'écriture utile est la SECONDE occurrence (handle le plus haut, `service000c`
    sous BlueZ). Comme les caractéristiques ont des UUID **en double**, tout doit être
    adressé **par handle** : sans ça, bleak lève
    `Multiple Characteristics with this UUID, refer to your desired characteristic by
    the handle attribute instead` (vécu pour l'écriture ET pour les notifications) ;
  * les écritures doivent être faites **avec réponse** (`response=True`), comme le script
    qui a réellement imprimé : en `response=False` l'imprimante accepte les octets sans
    broncher mais n'imprime rien — panne silencieuse, sans la moindre exception ;
  * le flux est régulé par des crédits envoyés sur `ff03` (`01 05` = 5 paquets). Si les
    crédits n'arrivent pas, l'attente ne doit pas bloquer l'impression : elle est donc
    abandonnée après quelques échecs.

⚠️ Le coût d'un aller-retour par paquet via proxy est de l'ordre de la centaine de ms :
la taille des tranches doit être choisie pour qu'une tranche reste sous la tolérance de
pause de l'imprimante (400 ms mesurées). Mesurer avec `s002_printer.diagnose` plutôt que
de supposer.
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
    DEFAULT_WRITE_RESPONSE,
    FLOW_TIMEOUT_MS,
    FLOW_WINDOW,
    NOTIFY_FLOW_UUID,
    NOTIFY_STATE_UUID,
    SERVICE_UUID,
    WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)

# Nombre d'échecs consécutifs d'attente de crédit avant d'abandonner le contrôle de flux
# pour le reste de l'impression (l'imprimante tamponne ; mieux vaut avancer que caler).
FLOW_ABANDON_APRES = 3


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
    chunk_ms: list[float] = field(default_factory=list)
    flow_waits: int = 0
    flow_timeout: int = 0
    notify_state: int = 0
    notify_flow: int = 0
    notify_errors: list[str] = field(default_factory=list)
    write_response: bool = DEFAULT_WRITE_RESPONSE
    channel_handle: int | None = None
    channels_seen: list[str] = field(default_factory=list)
    # Chemin retenu : quel scanner porte la connexion et à quelle force de signal.
    scanner_source: str = ""
    scanner_rssi: int | None = None
    scanner_candidats: list[str] = field(default_factory=list)
    # Tailles de trame réellement émises (l'adaptatif doit rester sous la tolérance).
    frame_lines: list[int] = field(default_factory=list)
    # Ce que l'imprimante DIT d'elle-même (trame d'état 0x10) : sans ces valeurs, un
    # « rien n'a été imprimé » reste inexplicable alors que le transport a réussi.
    state_payloads: list[str] = field(default_factory=list)
    battery_pct: int | None = None

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
            "avg_chunk_ms": round(sum(self.chunk_ms) / len(self.chunk_ms), 1) if self.chunk_ms else 0.0,
            "max_chunk_ms": round(max(self.chunk_ms), 1) if self.chunk_ms else 0.0,
            "throughput_kbps": round(self.bytes_written / total, 2) if total else 0.0,
            "flow_waits": self.flow_waits,
            "flow_timeout": self.flow_timeout,
            "notify_state": self.notify_state,
            "notify_flow": self.notify_flow,
            "notify_errors": self.notify_errors,
            "write_response": self.write_response,
            "channel_handle": self.channel_handle,
            "channels_seen": self.channels_seen,
            "scanner_source": self.scanner_source,
            "scanner_rssi": self.scanner_rssi,
            "scanner_candidats": self.scanner_candidats,
            "min_frame_lines": min(self.frame_lines) if self.frame_lines else None,
            "max_frame_lines": max(self.frame_lines) if self.frame_lines else None,
            "state_payloads": self.state_payloads[-4:],
            "battery_pct": self.battery_pct,
        }


class S002Transport:
    """Connexion BLE, écriture des trames et mesure du débit."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str = "S002",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        write_response: bool = DEFAULT_WRITE_RESPONSE,
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self.name = name
        # int() : les sélecteurs numériques de HA renvoient des flottants (cf. yk.chunks).
        self.chunk_size = max(20, min(237, int(chunk_size)))  # MTU 240 → 237 utiles au max
        self.write_response = bool(write_response)
        self.stats = WriteStats(write_response=self.write_response)
        self._client: BleakClientWithServiceCache | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._flow_char: BleakGATTCharacteristic | None = None
        self._state_char: BleakGATTCharacteristic | None = None
        self._credits = 0
        self._credit_event = asyncio.Event()
        self._flux_abandonne = False

    # ------------------------------------------------------------------ connexion
    async def connect(self) -> None:
        """Établit la connexion (routée par HA : proxy ou adaptateur local)."""
        device, source, rssi = self._meilleur_chemin()
        self.stats.scanner_source = source
        self.stats.scanner_rssi = rssi
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
        _LOGGER.info("S002 %s : connecté en %.0f ms", self.address, self.stats.connect_ms)
        self._select_channels()
        await self._subscribe_notifications()

    def _meilleur_chemin(self) -> tuple[object | None, str, int | None]:
        """Choisit le scanner le plus FORT parmi ceux qui voient l'imprimante.

        `async_ble_device_from_address` retient le scanner dont l'annonce est la plus
        RÉCENTE, pas la plus forte : mesuré sur ce parc, HA est passé par un proxy à
        **−99 dBm** (batcave) au lieu d'un autre à **−84 dBm** (cuisine), ce qui a doublé
        le coût par paquet (97 ms contre 54) et fait dépasser la tolérance de pause de
        l'imprimante → blancs de 4 mm. On va donc chercher la liste complète des
        scanners (`async_scanner_devices_by_address`) et on trie sur le RSSI.
        """
        candidats: list[tuple[int, str, object]] = []
        try:
            for vu in bluetooth.async_scanner_devices_by_address(
                self.hass, self.address, connectable=True
            ):
                rssi = getattr(vu.advertisement, "rssi", None)
                source = (
                    getattr(getattr(vu, "scanner", None), "source", None)
                    or getattr(getattr(vu, "scanner", None), "name", None)
                    or "?"
                )
                candidats.append((rssi if rssi is not None else -127, str(source), vu))
        except Exception as err:  # noqa: BLE001 — API indisponible : on retombe sur l'autoroute HA
            _LOGGER.debug("S002 %s : liste des scanners indisponible (%s)", self.address, err)

        if candidats:
            candidats.sort(key=lambda c: -c[0])
            self.stats.scanner_candidats = [f"{s} {r} dBm" for r, s, _ in candidats]
            rssi, source, vu = candidats[0]
            # L'appareil est dans `.ble_device` (habluetooth 6.26.11, la version épinglée
            # par HA) — `.device` n'existe pas : sans le repli on concluait « introuvable »
            # alors que le scanner venait de le voir (erreur vécue en v0.1.8).
            appareil = getattr(vu, "ble_device", None) or getattr(vu, "device", None)
            if appareil is not None:
                _LOGGER.info(
                    "S002 %s : chemin retenu %s (%s dBm) parmi %s",
                    self.address, source, rssi, self.stats.scanner_candidats,
                )
                return appareil, source, (None if rssi == -127 else rssi)
            _LOGGER.warning(
                "S002 %s : %s voit l'imprimante mais aucun champ d'appareil exploitable "
                "(%s) — repli sur l'autoroute HA",
                self.address, source, type(vu).__name__,
            )

        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        return device, "autoroute HA", None

    def _select_channels(self) -> None:
        """Choisit les caractéristiques de la SECONDE occurrence du service ff00.

        Le service ff00 existe en double : ses caractéristiques ont donc des UUID
        identiques et bleak exige qu'on les désigne par handle. On regroupe par instance
        de service et on retient celle dont le handle est le plus élevé — c'est celle qui
        imprime (vérifié sur le matériel : `service000c` sous BlueZ).
        """
        assert self._client is not None
        instances: dict[int, dict[str, BleakGATTCharacteristic]] = {}
        for service in self._client.services:
            if service.uuid.lower() != SERVICE_UUID:
                continue
            cle = service.handle if hasattr(service, "handle") else id(service)
            for char in service.characteristics:
                uuid = char.uuid.lower()
                if uuid in (WRITE_UUID, NOTIFY_FLOW_UUID, NOTIFY_STATE_UUID):
                    instances.setdefault(cle, {})[uuid] = char

        self.stats.channels_seen = [
            f"instance service={cle} : "
            + ", ".join(
                f"{u.split('-')[0]}=0x{c.handle:04x}" for u, c in sorted(chars.items())
            )
            for cle, chars in sorted(instances.items(), reverse=True)
        ]
        if not instances:
            raise S002Error(
                f"Service {SERVICE_UUID} absent : services vus = "
                f"{[s.uuid for s in self._client.services]}"
            )
        # Instance au handle de service le plus élevé = la seconde occurrence.
        chars = instances[max(instances)]
        self._write_char = chars.get(WRITE_UUID)
        self._flow_char = chars.get(NOTIFY_FLOW_UUID)
        self._state_char = chars.get(NOTIFY_STATE_UUID)
        if self._write_char is None:
            raise S002Error(f"Caractéristique d'écriture {WRITE_UUID} absente : {self.stats.channels_seen}")
        self.stats.channel_handle = self._write_char.handle
        _LOGGER.info(
            "S002 : canaux retenus (write=0x%04x, flux=0x%04x, état=0x%04x) parmi %s",
            self._write_char.handle,
            self._flow_char.handle if self._flow_char else 0,
            self._state_char.handle if self._state_char else 0,
            self.stats.channels_seen,
        )

    async def _subscribe_notifications(self) -> None:
        """S'abonne à l'état (ff01) et au contrôle de flux (ff03), PAR HANDLE."""
        assert self._client is not None

        def _on_flow(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            self.stats.notify_flow += 1
            if len(data) >= 1 and data[0] == 0x01:
                self._credits += 1
                self._credit_event.set()

        def _on_state(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            self.stats.notify_state += 1
            brut = bytes(data)
            self.stats.state_payloads.append(brut.hex(" "))
            _LOGGER.debug("S002 état : %s", brut.hex(" "))
            # Format mesuré (cf. skill) : payload[7] = batterie en % (0x35 = 53 %).
            # On lit large : en-tête de 5 octets, contrôle de 5 octets en queue.
            if len(brut) >= 13:
                charge = brut[5:-5][7] if len(brut[5:-5]) > 7 else None
                if charge is not None and 0 < charge <= 100:
                    self.stats.battery_pct = charge

        for char, callback, nom in (
            (self._flow_char, _on_flow, "flux ff03"),
            (self._state_char, _on_state, "état ff01"),
        ):
            if char is None:
                self.stats.notify_errors.append(f"{nom} : caractéristique absente")
                continue
            try:
                # On passe la CARACTÉRISTIQUE (pas l'UUID) : les UUID sont en double.
                await self._client.start_notify(char, callback)
                _LOGGER.debug("S002 : abonné à %s (handle 0x%04x)", nom, char.handle)
            except Exception as err:  # noqa: BLE001 - l'absence de notify ne doit pas bloquer
                self.stats.notify_errors.append(f"{nom}: {type(err).__name__}: {err}")
                _LOGGER.warning("S002 : abonnement %s impossible (%s)", nom, err)

    # ------------------------------------------------------------------ écriture
    async def _write_chunk(self, morceau: bytes) -> None:
        assert self._client is not None and self._write_char is not None
        debut = time.monotonic()
        # Écriture AVEC réponse par défaut : en `response=False` l'imprimante reçoit les
        # octets mais n'imprime rien (vécu). Cf. l'en-tête du module.
        await self._client.write_gatt_char(
            self._write_char, morceau, response=self.write_response
        )
        self.stats.chunk_ms.append((time.monotonic() - debut) * 1000)
        self.stats.chunks += 1
        self.stats.bytes_written += len(morceau)

    async def write_frame(self, trame: bytes, chunk_size: int | None = None) -> float:
        """Écrit une trame complète en respectant (si possible) le contrôle de flux."""
        taille = chunk_size or self.chunk_size
        morceaux = yk.chunks(trame, taille)
        debut = time.monotonic()
        for i, morceau in enumerate(morceaux, start=1):
            await self._write_chunk(morceau)
            if self._flux_abandonne or i % FLOW_WINDOW or i >= len(morceaux):
                continue
            self._credit_event.clear()
            avant = self._credits
            try:
                await asyncio.wait_for(
                    self._attendre_credit(avant), timeout=FLOW_TIMEOUT_MS / 1000
                )
                self.stats.flow_waits += 1
            except asyncio.TimeoutError:
                self.stats.flow_timeout += 1
                if self.stats.flow_timeout >= FLOW_ABANDON_APRES:
                    self._flux_abandonne = True
                    _LOGGER.info(
                        "S002 : contrôle de flux abandonné (%d attentes sans crédit) — "
                        "l'imprimante tamponne, on continue sans caler",
                        self.stats.flow_timeout,
                    )
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
