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
from .trame import faut_reappliquer

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
        #: Dernière intention de l'utilisateur sur le maintien de liaison (None = jamais exprimée).
        #: L'interrupteur lit son ÉTAT sur le nœud, donc HA n'a aucun souvenir par lui-même : sans
        #: cette intention, un redémarrage du nœud perd le maintien sans que personne ne s'en
        #: aperçoive. Elle est restaurée au démarrage de HA par l'interrupteur (`RestoreEntity`).
        self.voulu_maintien: bool | None = None

    async def _async_update_data(self) -> dict:
        """Une interrogation du nœud. Lève UpdateFailed pour laisser l'entité « indisponible ».

        On ne remonte JAMAIS un dictionnaire vide : une entité qui afficherait « 0 % » alors que
        la mesure a échoué serait pire qu'une entité indisponible (leçon du watchdog lavante :
        une absence de donnée doit se VOIR).
        """
        try:
            etat = await self.imprimante.etat_noeud()
        except S002Error as err:
            raise UpdateFailed(str(err)) from err

        # Le nœud a-t-il perdu le maintien (redémarrage, OTA, BROWNOUT) alors que l'utilisateur
        # l'avait demandé ? On le rétablit, puis on RELIT pour que les entités montrent la vérité.
        if faut_reappliquer(self.voulu_maintien, etat):
            _LOGGER.info("Le nœud ne maintient plus la liaison : rétablissement demandé")
            try:
                await self.imprimante.maintenir_liaison(True)
                etat = await self.imprimante.etat_noeud()
            except S002Error as err:
                _LOGGER.warning("Rétablissement du maintien impossible : %s", err)
        return etat
