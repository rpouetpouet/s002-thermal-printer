#!/usr/bin/env python3
"""Test des entités v0.3.0 : batterie, mode manuel/auto, bouton « libérer le bluetooth ».

Ce qu'on vérifie ici est exactement ce que l'utilisateur verra : la VALEUR affichée par le
capteur et la COMMANDE envoyée au nœud par l'interrupteur et le bouton. Ni Home Assistant en
marche, ni imprimante, ni nœud ne sont nécessaires.

Exécution (il faut un environnement où `homeassistant` est installé) :

    ~/.hermes/cache/scratch/venv_ha/bin/python tests/test_entites_v030.py

Pourquoi ce fichier existe : le reste de la suite tourne avec Home Assistant absent. Ici on a
besoin des vraies classes d'entités de HA, donc des vrais `CoordinatorEntity`/`SensorEntity` —
c'est la seule façon de garantir que `native_value` et `is_on` se comportent comme prévu plutôt
que de le supposer.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

echecs: list[str] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    print(("  OK    " if condition else "  ECHEC ") + nom + ("" if condition else f" — {detail}"))
    if not condition:
        echecs.append(nom)


try:
    from custom_components.s002_printer import button, sensor, switch  # noqa: E402
except ModuleNotFoundError as err:  # pragma: no cover
    print(f"  IGNORE : Home Assistant n'est pas installé ici ({err})")
    print("           exécuter ce fichier avec un venv qui contient homeassistant")
    raise SystemExit(0)


class FauxImprimante:
    """Retient ce qu'on lui demande — c'est la preuve que l'entité appelle la bonne chose."""

    address = "06:03:DD:EC:16:4D"
    name = "S002"

    def __init__(self) -> None:
        self.appels: list[tuple[str, object]] = []

    async def liberer_bluetooth(self) -> str:
        self.appels.append(("liberer", None))
        return "LIBERE"

    async def maintenir_liaison(self, actif: bool) -> str:
        self.appels.append(("maintenir", actif))
        return "MAINTIEN ACTIF" if actif else "MAINTIEN INACTIF"


class FauxCoordinator:
    """Le minimum que `CoordinatorEntity` touche réellement à la construction."""

    def __init__(self, data: dict, imprimante: FauxImprimante | None = None) -> None:
        self.data = data
        self.imprimante = imprimante or FauxImprimante()
        self.last_update_success = True
        self.rafraichi = False

    def async_add_listener(self, *_args, **_kwargs):  # noqa: ANN002
        return lambda: None

    async def async_request_refresh(self) -> None:
        self.rafraichi = True


STATUT_NOMINAL = {
    "etat": "pret",
    "liaison": "tenue",
    "mode": "manuel",
    "maintien": "oui",
    "batterie": 47,
    "etats": 12,
    "partition": "ota_1",
}


print("=== 1. Capteur de batterie ===")
coord = FauxCoordinator(dict(STATUT_NOMINAL))
bat = sensor.S002Batterie(coord)
verifier("batterie=47 affiche 47", bat.native_value == 47, f"obtenu {bat.native_value!r}")
verifier("unité en pourcent", str(bat._attr_native_unit_of_measurement) in ("%", "PERCENTAGE"),
         str(bat._attr_native_unit_of_measurement))
verifier("classe d'appareil batterie", str(bat._attr_device_class).lower().endswith("battery"),
         str(bat._attr_device_class))
verifier("identifiant unique stable", bat.unique_id == "06:03:DD:EC:16:4D_batterie",
         str(bat.unique_id))

coord_b = FauxCoordinator({**STATUT_NOMINAL, "batterie": -1, "etats": 0})
bat_b = sensor.S002Batterie(coord_b)
verifier("batterie=-1 (aucune trame reçue) affiche « inconnu », pas 0 %", bat_b.native_value is None,
         f"obtenu {bat_b.native_value!r}")
verifier("le capteur explique POURQUOI il n'a pas de valeur",
         bool(bat_b.extra_state_attributes.get("remarque")))

coord_c = FauxCoordinator({"etat": "attente"})
verifier("champ absent → inconnu (jamais d'exception)",
         sensor.S002Batterie(coord_c).native_value is None)


print("=== 2. Interrupteur manuel / auto ===")
coord2 = FauxCoordinator(dict(STATUT_NOMINAL))
inter = switch.S002MaintenirLiaison(coord2)
verifier("maintien=oui → allumé (mode manuel)", inter.is_on is True)

coord3 = FauxCoordinator({**STATUT_NOMINAL, "maintien": "non", "mode": "auto"})
inter3 = switch.S002MaintenirLiaison(coord3)
verifier("maintien=non → éteint (mode auto)", inter3.is_on is False)
verifier("l'état vient du nœud, pas d'un souvenir local",
         str(inter3._etat_noeud.get("mode")) == "auto")


async def scenario_bascule() -> tuple[bool, bool]:
    imprimante = FauxImprimante()
    coord = FauxCoordinator({**STATUT_NOMINAL, "maintien": "non"}, imprimante)
    inter = switch.S002MaintenirLiaison(coord)
    await inter.async_turn_on()
    await inter.async_turn_off()
    return (
        imprimante.appels == [("maintenir", True), ("maintenir", False)],
        coord.rafraichi,
    )


print("=== 3. Commandes réellement envoyées ===")
ok_appels, ok_refresh = asyncio.run(scenario_bascule())
verifier("allumer puis éteindre envoie MAINTENIR 1 puis MAINTENIR 0", ok_appels)
verifier("l'entité redemande un relevé après la bascule", ok_refresh)


async def scenario_bouton() -> tuple[list, bool]:
    imprimante = FauxImprimante()
    coord = FauxCoordinator(dict(STATUT_NOMINAL), imprimante)
    await button.S002LibererBluetooth(coord).async_press()
    return imprimante.appels, coord.rafraichi


appels, rafraichi = asyncio.run(scenario_bouton())
verifier("le bouton envoie LIBERER", appels == [("liberer", None)], str(appels))
verifier("le bouton redemande un relevé (l'état redevient « libre »)", rafraichi)

print()
print("=" * 70)
if echecs:
    print(f"ECHECS : {len(echecs)} — " + ", ".join(echecs))
    raise SystemExit(1)
print("TOUS LES TESTS PASSENT — entités conformes au comportement attendu")
