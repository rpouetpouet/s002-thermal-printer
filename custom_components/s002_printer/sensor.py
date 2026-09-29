"""Capteur de batterie : la valeur que le nœud lisait sans jamais la remonter.

L'imprimante pousse sa trame d'état (18 octets) toutes les 5 s ; le nœud la JETAIT, donc la
batterie restait inconnue en mode « node » alors que le chemin proxy la lisait très bien.
Depuis le firmware v3, le nœud extrait la charge (octet 12, même règle que `ble.py`) et
l'expose dans son STATUS.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHAMP_BATTERIE, CHAMP_ETATS, CHAMP_LIAISON, DOMAIN
from .coordinator import S002Coordinator
from .entity import S002Entite


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: S002Coordinator = hass.data[DOMAIN + "_coord"][entry.entry_id]
    async_add_entities([S002Batterie(coordinator)])


class S002Batterie(S002Entite, SensorEntity):
    """Niveau de batterie annoncé par l'imprimante dans sa trame d'état."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: S002Coordinator) -> None:
        super().__init__(coordinator, "batterie", "Batterie")

    @property
    def native_value(self) -> int | None:
        """Pourcentage, ou `None` tant qu'aucune trame d'état n'a été reçue.

        Le nœud renvoie -1 tant qu'il n'a rien vu : afficher « 0 % » serait un mensonge (et
        ferait croire à une batterie vide). `None` = inconnu, c'est visible dans HA.
        """
        valeur = self._etat_noeud.get(CHAMP_BATTERIE)
        if isinstance(valeur, int) and valeur >= 0:
            return valeur
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """De quoi comprendre une valeur absente sans se connecter au nœud."""
        etats = self._etat_noeud.get(CHAMP_ETATS)
        return {
            "trames_etat_recues": etats,
            "liaison": self._etat_noeud.get(CHAMP_LIAISON),
            "remarque": (
                "aucune trame d'état reçue depuis le démarrage du nœud : la batterie n'est "
                "connue que lorsque la liaison Bluetooth est tenue (l'imprimante n'envoie sa "
                "trame d'état que dans ce cas)"
            )
            if not isinstance(etats, int) or etats == 0
            else None,
        }
