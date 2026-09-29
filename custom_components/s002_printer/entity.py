"""Base commune aux entités S002 : elles partagent le même appareil et le même coordinateur."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import S002Coordinator


def appareil(coordinator: S002Coordinator) -> DeviceInfo:
    """Fiche de l'appareil : les entités d'une même entrée sont regroupées dessous."""
    imprimante = coordinator.imprimante
    return DeviceInfo(
        identifiers={(DOMAIN, imprimante.address)},
        name=f"S002 {imprimante.name}",
        manufacturer="ORGSTA",
        model="S002 (imprimante thermique 57 mm)",
        # Le nœud réseau est ce qui rend ces entités possibles : on le nomme, sinon on croirait
        # que la batterie vient de l'imprimante elle-même (elle vient de sa trame d'état, relayée).
        configuration_url=None,
        via_device=None,
        sw_version=None,
    )


class S002Entite(CoordinatorEntity[S002Coordinator]):
    """Entité rattachée au nœud, avec la fiche appareil et un nom préfixé."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: S002Coordinator, cle: str, nom: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.imprimante.address}_{cle}"
        self._attr_name = nom
        self._attr_device_info = appareil(coordinator)

    @property
    def _etat_noeud(self) -> dict:
        return self.coordinator.data or {}
