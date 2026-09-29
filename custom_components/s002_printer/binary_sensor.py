"""Capteur « l'imprimante a refusé d'imprimer ».

POURQUOI : le service d'impression rapporte `errors=[]` même quand rien ne sort — vérifié le
29/09/2026, bobine vide, impression impossible, rapport sans erreur. L'utilisateur ne l'apprend
qu'en regardant le papier.

D'où vient l'information : l'imprimante pousse une trame d'état de 18 octets, et le nœud la publie
en clair dans le champ `brut` du STATUS. En comparant une capture AVEC papier et une capture SANS
papier, un seul bit bascule :

    avec papier :  ... 07 03 90 ...      charge[2] = 0x03
    sans papier :  ... 08 0b 88 ...      charge[2] = 0x0b   <-- +0x08
    papier remis : ... 08 03 90 ...      charge[2] = 0x03   <-- bit retombé

Le bit 0x08 de `charge[2]` (octet 7 de la trame, en-tête de 5 octets) est donc le seul indicateur
observé. DEUX RÉSERVES, mesurées elles aussi :

- il ne s'allume **qu'au moment où l'imprimante essaie d'imprimer** : une bobine vide ne se voit pas
  tant qu'aucune impression n'est tentée. C'est un témoin d'échec, pas un détecteur préventif ;
- il peut signaler un refus d'imprimer en général (bourrage, tête en surchauffe), pas seulement le
  papier. Le nom de l'entité reste donc prudent.

L'octet 1 de la charge (`07` -> `08` sans papier) ne revient PAS en arrière quand le papier
revient : c'est un témoin collant, volontairement non utilisé ici.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHAMP_BRUT, DOMAIN
from .coordinator import S002Coordinator
from .entity import S002Entite
from .trame import BIT_REFUS, OCTET_REFUS, octets as octets_trame, refus_impression


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: S002Coordinator = hass.data[DOMAIN + "_coord"][entry.entry_id]
    async_add_entities([S002ImpressionRefusee(coordinator)])


class S002ImpressionRefusee(S002Entite, BinarySensorEntity):
    """Allumé = l'imprimante a refusé la dernière impression tentée."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: S002Coordinator) -> None:
        super().__init__(coordinator, "impression_refusee", "Impression refusée")

    @property
    def _trame(self) -> str:
        return str(self._etat_noeud.get(CHAMP_BRUT) or "")

    @property
    def is_on(self) -> bool | None:
        """Vrai si le bit de refus est allumé ; None si la trame n'est pas lisible."""
        return refus_impression(self._trame)

    @property
    def extra_state_attributes(self) -> dict:
        octets = octets_trame(self._trame)
        return {
            "octet_surveille": f"charge[2] = {octets[OCTET_REFUS] if len(octets) > OCTET_REFUS else '?'}",
            "bit_surveille": f"0x{BIT_REFUS:02x}",
            "trame_etat": " ".join(octets) or None,
            "explication": (
                "s'allume quand l'imprimante refuse une impression (bobine vide, par exemple). "
                "Ne se déclenche qu'à une tentative d'impression, pas avant."
            ),
        }
