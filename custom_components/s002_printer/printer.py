"""Séquence d'impression d'une image sur l'ORGSTA S002.

Ordre impératif, validé sur le matériel (image continue, cadre fermé) :

    1. trame `0x80` token, payload `01`          → ouvre la session BLE
    2. trame `0x0f` largeur (uint16 LE, 576)     → l'imprimante en déduit 72 o/ligne
    3. trames `0x00` par tranche de 40 lignes    → DOS À DOS (voir contrainte ci-dessous)
    4. trame `0x02` avance papier (optionnelle)

⚠️ CONTRAINTE CRITIQUE : une pause de ~1 s entre deux tranches fait considérer la tâche
comme terminée et l'imprimante avance 48 lignes (4 mm) de blanc, ce qui hache l'image.
Mesuré : 400 ms tolérées, 1 000 ms non. Par défaut on envoie donc dos à dos (0 ms).

⚠️ Une seule impression à la fois par imprimante : deux impressions entrelacées garberaient
les trames (verrou par adresse, comme dans `ha-escpos-thermal-printer`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field

from homeassistant.core import HomeAssistant

from . import yk
from .ble import S002Transport, S002Error
from .const import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_NODE_PORT,
    DEFAULT_TRANSPORT,
    DEFAULT_FRAME_PAUSE_MS,
    FRAME_BUDGET_MS,
    TRANSPORT_NODE,
    DEFAULT_WRITE_RESPONSE,
    MAX_LINES_PER_FRAME,
    MSG_IMAGE_SLICE,
    PRINT_WIDTH_DOTS,
    SETTLE_AFTER_TOKEN_MS,
    SETTLE_AFTER_WIDTH_MS,
)

_LOGGER = logging.getLogger(__name__)

# Un verrou par imprimante : deux impressions simultanées seraient entrelacées.
_VERROUS: dict[str, asyncio.Lock] = {}


def _verrou(address: str) -> asyncio.Lock:
    return _VERROUS.setdefault(address.upper(), asyncio.Lock())


@dataclass
class PrintResult:
    """Compte rendu d'impression, renvoyé tel quel par les services HA."""

    address: str
    frames: int = 0
    bytes_sent: int = 0
    duration_s: float = 0.0
    raster_lines: int = 0
    height_mm: float = 0.0
    dry_run: bool = False
    timings: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class S002Printer:
    """Pilote une imprimante S002 (une instance par config entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str = "S002",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        write_response: bool = DEFAULT_WRITE_RESPONSE,
        frame_pause_ms: int = DEFAULT_FRAME_PAUSE_MS,
        lines_per_frame: int = MAX_LINES_PER_FRAME,
        frame_budget_ms: int = FRAME_BUDGET_MS,
        feed_before_mm: float = 0.0,
        feed_after_mm: float = 0.0,
        transport: str = DEFAULT_TRANSPORT,
        node_host: str = "",
        node_port: int = DEFAULT_NODE_PORT,
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self.name = name
        # int() : les sélecteurs numériques de HA renvoient des flottants (cf. yk.chunks).
        self.chunk_size = int(chunk_size)
        self.write_response = bool(write_response)
        self.frame_pause_ms = int(frame_pause_ms)
        self.lines_per_frame = max(1, min(MAX_LINES_PER_FRAME, int(lines_per_frame)))
        # Plafond de durée visé par trame ; `lines_per_frame` reste le maximum protocolaire.
        self.frame_budget_ms = max(50, int(frame_budget_ms))
        self.feed_before_mm = feed_before_mm
        self.feed_after_mm = feed_after_mm
        self.transport = transport
        self.node_host = (node_host or "").strip()
        self.node_port = int(node_port)

    def _creer_transport(self, forcer_ble: bool = False):
        """Fabrique le transport à utiliser pour cette impression.

        Le reste du code ne connaît pas la différence : les deux transports exposent
        `connect()`, `write_frame()` et `stats`. Le choix se fait par entrée (option
        « transport »), ce qui permet de basculer entre le proxy BLE et le nœud réseau dédié
        sans réinstaller l'intégration.
        """
        if not forcer_ble and self.transport == TRANSPORT_NODE:
            if not self.node_host:
                raise S002Error(
                    "transport « node » sélectionné mais aucune adresse de nœud renseignée "
                    "(option « node_host »)"
                )
            # Import paresseux : le chemin Bluetooth reste utilisable même si ce module
            # avait un problème (et inversement).
            from .node import S002NodeTransport

            return S002NodeTransport(self.node_host, self.node_port)
        return S002Transport(
            self.hass, self.address, self.name, self.chunk_size, self.write_response
        )

    async def print_raster(self, raster: bytes, dry_run: bool = False) -> PrintResult:
        """Imprime un raster 1 bit (1 = noir, bit de poids fort = premier point)."""
        if not raster:
            raise S002Error("raster vide")
        if len(raster) % yk.BYTES_PER_LINE:
            raster = b"".join(yk.split_lines(raster))

        stats_raster = yk.raster_stats(raster)
        resultat = PrintResult(
            address=self.address,
            raster_lines=stats_raster["lines"],
            height_mm=stats_raster["height_mm"],
            dry_run=dry_run,
        )

        if dry_run:
            _LOGGER.info(
                "S002 %s : simulation (%d lignes, %.1f mm) — rien n'est envoyé",
                self.address,
                resultat.raster_lines,
                resultat.height_mm,
            )
            return resultat

        async with _verrou(self.address):
            debut = time.monotonic()
            transport = self._creer_transport()
            try:
                await transport.connect()

                # 1. token d'ouverture — la recette validée démarre le compteur à 1
                await transport.write_frame(yk.frame_token(1))
                await asyncio.sleep(SETTLE_AFTER_TOKEN_MS / 1000)
                # 2. largeur (l'imprimante en déduit 72 o/ligne)
                await transport.write_frame(yk.frame_paper_size(PRINT_WIDTH_DOTS, 2))
                await asyncio.sleep(SETTLE_AFTER_WIDTH_MS / 1000)

                compteur = 3
                if self.feed_before_mm > 0:
                    await transport.write_frame(yk.frame_feed(self.feed_before_mm, compteur))
                    compteur = (compteur + 1) & 0x3F
                    await asyncio.sleep(SETTLE_AFTER_WIDTH_MS / 1000)

                # 4. tranches d'image, dos à dos, TAILLE ADAPTATIVE
                # La latence du chemin (proxy BLE) varie du simple au triple selon le
                # scanner retenu et l'encombrement radio. Une trame fixe finit donc par
                # dépasser la tolérance de pause (~400 ms) → l'imprimante referme la
                # tâche et avance 4 mm de BLANC. On recalcule la taille après CHAQUE
                # trame à partir du pic de latence récent, en visant `frame_budget_ms`
                # et en ne descendant jamais sous une écriture (2 lignes pour 200 o).
                lignes = yk.split_lines(raster)
                lignes_par_ecriture = max(1, self.chunk_size // yk.BYTES_PER_LINE)
                ecritures_max = max(
                    1,
                    (self.lines_per_frame + lignes_par_ecriture - 1) // lignes_par_ecriture,
                )
                pic_ms = 60.0  # amorçage : ordre de grandeur mesuré sur un proxy moyen
                position = 0
                while position < len(lignes):
                    ecritures = max(
                        1,
                        min(ecritures_max, int(self.frame_budget_ms // max(pic_ms, 1.0))),
                    )
                    taille = min(len(lignes) - position, ecritures * lignes_par_ecriture)
                    payload = b"".join(lignes[position : position + taille])
                    trame = yk.build_frame(yk.MSG_IMAGE_SLICE, payload, compteur)
                    duree = await transport.write_frame(trame)
                    compteur = (compteur + 1) & 0x3F
                    transport.stats.frame_lines.append(taille)
                    _LOGGER.debug(
                        "S002 %s : tranche de %d lignes pour %d/%d lignes envoyée en %.0f ms "
                        "(pic récent %.0f ms/écriture)",
                        self.address, taille, position + taille, len(lignes), duree, pic_ms,
                    )
                    # Le PIC récent seul compte : c'est lui qui provoque les blancs.
                    if transport.stats.chunk_ms:
                        pic_ms = max(transport.stats.chunk_ms[-6:])
                    position += taille
                    if self.frame_pause_ms > 0:
                        await asyncio.sleep(self.frame_pause_ms / 1000)

                if self.feed_after_mm > 0:
                    await transport.write_frame(
                        yk.frame_feed(self.feed_after_mm, 0), chunk_size=64
                    )

                resultat.frames = transport.stats.frames
                resultat.bytes_sent = transport.stats.bytes_written
                resultat.timings = transport.stats.as_dict()
            except Exception as err:  # noqa: BLE001
                resultat.errors.append(f"{type(err).__name__}: {err}")
                _LOGGER.error("S002 %s : impression échouée — %s", self.address, err)
                raise
            finally:
                await transport.disconnect()
                resultat.duration_s = round(time.monotonic() - debut, 2)

            _LOGGER.info(
                "S002 %s : %d lignes (%.1f mm) imprimées en %.2f s — %d trames, %d octets, "
                "%.0f ms/trame en moyenne, canal 0x%s",
                self.address,
                resultat.raster_lines,
                resultat.height_mm,
                resultat.duration_s,
                resultat.frames,
                resultat.bytes_sent,
                resultat.timings.get("avg_frame_ms", 0),
                f"{resultat.timings.get('channel_handle') or 0:04x}",
            )
            return resultat

    async def print_text(
        self,
        lines: list[str],
        scale: int = 2,
        margin_dots: int = 16,
        dry_run: bool = False,
    ) -> PrintResult:
        """Imprime des lignes de texte (police bitmap de Pillow : aucune fonte à embarquer).

        `margin_dots` : marge symétrique gauche/droite en points. Par défaut 16 points
        (≈ 1,35 mm), c'est-à-dire des marges visuellement symétriques ; l'ancienne valeur
        de 8 ne laissait qu'une marge gauche et « décalait » le bloc (constaté à l'impression).
        Le texte trop large est replié automatiquement (`yk.text_raster`), il n'est donc plus
        tronqué en silence.
        """
        return await self.print_raster(
            yk.text_raster(lines, scale=scale, padding_dots=max(0, int(margin_dots))),
            dry_run=dry_run,
        )

    async def print_test_pattern(self, dry_run: bool = False) -> PrintResult:
        """Impression du motif de validation (repères de début/fin + 6 bandes distinctes)."""
        return await self.print_raster(yk.test_pattern_raster(), dry_run=dry_run)

    async def feed(self, mm: float) -> PrintResult:
        """Avance papier seule (unité ≈ 0,1 mm)."""
        resultat = PrintResult(address=self.address)
        async with _verrou(self.address):
            transport = self._creer_transport()
            try:
                await transport.connect()
                await transport.write_frame(yk.frame_token(0))
                await transport.write_frame(yk.frame_feed(mm, 1), chunk_size=64)
                resultat.frames = transport.stats.frames
                resultat.bytes_sent = transport.stats.bytes_written
                resultat.timings = transport.stats.as_dict()
            finally:
                await transport.disconnect()
        return resultat

    async def diagnose(self) -> dict:
        """Connexion sans impression : révèle quels canaux et crédits le lien expose.

        Sert de premier test depuis un proxy BLE : si les canaux `ff02` et les crédits
        de flux apparaissent, le passage par proxy est fonctionnel.
        """
        transport = self._creer_transport(forcer_ble=True)
        await transport.connect()
        try:
            # on ne consomme aucun papier : simple lecture de l'état annoncé
            await asyncio.sleep(6)   # l'imprimante pousse une trame d'état toutes les 5 s
            return transport.stats.as_dict()
        finally:
            await transport.disconnect()
