"""Bouton « libérer le Bluetooth » : rendre l'imprimante à l'instant.

Sans ordre explicite, il faut attendre le délai d'inactivité (120 s) avant qu'un téléphone
puisse se connecter. Ce bouton le fait tout de suite. Le nœud repasse alors en mode AUTO et
reprend l'imprimante de lui-même à la prochaine impression.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ble import S002Error
from .const import CHAMP_LIAISON, DOMAIN
from .coordinator import S002Coordinator
from .entity import S002Entite

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: S002Coordinator = hass.data[DOMAIN + "_coord"][entry.entry_id]
    async_add_entities([S002LibererBluetooth(coordinator)])


class S002LibererBluetooth(S002Entite, ButtonEntity):
    """Rend la liaison Bluetooth immédiatement."""

    _attr_icon = "mdi:bluetooth-off"

    def __init__(self, coordinator: S002Coordinator) -> None:
        super().__init__(coordinator, "liberer_bluetooth", "Libérer le Bluetooth")

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "liaison": self._etat_noeud.get(CHAMP_LIAISON),
            "explication": (
                "l'imprimante n'accepte qu'un client à la fois : ce bouton libère la liaison pour "
                "qu'un téléphone (app Snap and Tag) puisse s'y connecter. Le nœud la reprend "
                "automatiquement à la prochaine impression."
            ),
        }

    async def async_press(self) -> None:
        try:
            await self.coordinator.imprimante.liberer_bluetooth()
        except S002Error as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
