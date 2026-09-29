"""Interrupteur « maintenir la liaison » : mode manuel ou mode auto.

Le nœud rend l'imprimante après un délai d'inactivité (120 s par défaut) pour qu'un téléphone
puisse s'y connecter. En mode MANUEL, il la garde et ignore ce délai — utile juste avant une
série d'impressions, ou pour que l'app du téléphone ne prenne pas la main.
"""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ble import S002Error
from .const import CHAMP_LIAISON, CHAMP_MAINTIEN, DOMAIN
from .coordinator import S002Coordinator
from .entity import S002Entite

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: S002Coordinator = hass.data[DOMAIN + "_coord"][entry.entry_id]
    async_add_entities([S002MaintenirLiaison(coordinator)])


class S002MaintenirLiaison(S002Entite, SwitchEntity):
    """Allumé = mode manuel (liaison gardée), éteint = mode auto (délai d'inactivité)."""

    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coordinator: S002Coordinator) -> None:
        super().__init__(coordinator, "maintenir_liaison", "Maintenir la liaison")

    @property
    def is_on(self) -> bool:
        """L'état vient du NŒUD, pas d'un souvenir local.

        C'est ce qui rend l'interrupteur honnête : le bouton « Libérer le Bluetooth » repasse le
        nœud en mode auto, et l'interrupteur se remet donc sur « éteint » de lui-même.
        """
        return str(self._etat_noeud.get(CHAMP_MAINTIEN, "")).lower() in ("oui", "true", "1")

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "mode": self._etat_noeud.get("mode"),
            "liaison": self._etat_noeud.get(CHAMP_LIAISON),
            "explication": (
                "allumé : le nœud garde la liaison et ignore le délai d'inactivité ; "
                "éteint : il la rend après le délai, ce qui laisse un téléphone se connecter"
            ),
        }

    async def _basculer(self, actif: bool) -> None:
        try:
            await self.coordinator.imprimante.maintenir_liaison(actif)
        except S002Error as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **_kwargs) -> None:
        await self._basculer(True)

    async def async_turn_off(self, **_kwargs) -> None:
        await self._basculer(False)
