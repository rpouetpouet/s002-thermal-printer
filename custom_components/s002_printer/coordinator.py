"""Coordinateur : interroge le nœud S002 à intervalle régulier.

Pourquoi un coordinateur plutôt qu'une interrogation par entité : trois entités (batterie,
mode de liaison, et l'état de liaison) lisent la MÊME réponse STATUS. Le coordinateur la
demande une fois et la partage — et surtout, il la demande sous le verrou de l'imprimante, donc
jamais pendant une impression.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import S002Error
from .const import DOMAIN, NODE_STATUT_INTERVALLE_S
from .printer import S002Printer

_LOGGER = logging.getLogger(__name__)


class S002Coordinator(DataUpdateCoordinator[dict]):
    """Suit l'état du nœud (batterie, mode, liaison)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, imprimante: S002Printer) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{imprimante.address}",
            update_interval=timedelta(seconds=NODE_STATUT_INTERVALLE_S),
        )
        self.entry = entry
        self.imprimante = imprimante

    async def _async_update_data(self) -> dict:
        """Une interrogation du nœud. Lève UpdateFailed pour laisser l'entité « indisponible ».

        On ne remonte JAMAIS un dictionnaire vide : une entité qui afficherait « 0 % » alors que
        la mesure a échoué serait pire qu'une entité indisponible (leçon du watchdog lavante :
        une absence de donnée doit se VOIR).
        """
        try:
            return await self.imprimante.etat_noeud()
        except S002Error as err:
            raise UpdateFailed(str(err)) from err
