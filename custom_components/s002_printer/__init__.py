"""Intégration Home Assistant pour l'imprimante thermique ORGSTA S002 (protocole YK/CUS).

Phase 1 : connexion via la pile Bluetooth de HA (donc **compatible proxies BLE ESP32**),
services d'impression, et journalisation du débit — c'est cette mesure qui dira si le
passage par proxy tient la charge.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADDRESS,
    CONF_CHUNK_SIZE,
    CONF_WRITE_RESPONSE,
    CONF_FEED_AFTER_MM,
    CONF_FEED_BEFORE_MM,
    CONF_FRAME_PAUSE_MS,
    CONF_NAME,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_FRAME_PAUSE_MS,
    DEFAULT_LINES_PER_FRAME,
    DEFAULT_WRITE_RESPONSE,
    DOMAIN,
    MAX_LINES_PER_FRAME,
)
from .printer import S002Printer

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = []

SERVICE_PRINT_TEST = "print_test"
SERVICE_PRINT_TEXT = "print_text"
SERVICE_PRINT_RAW = "print_raw"
SERVICE_PRINT_IMAGE = "print_image"
SERVICE_FEED = "feed"
SERVICE_DIAGNOSE = "diagnose"

SERVICE_BASE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ADDRESS): cv.string,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

PRINT_TEXT_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Required("text"): cv.string,
        vol.Optional("scale", default=2): vol.All(vol.Coerce(int), vol.Range(min=1, max=6)),
        # Marge symétrique gauche/droite, en points (16 ≈ 1,35 mm).
        vol.Optional("margin_dots", default=16): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=200)
        ),
    }
)

FEED_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {vol.Required("mm", default=10): vol.All(vol.Coerce(float), vol.Range(min=1, max=200))}
)

# `data` = raster brut (72 octets par ligne, 1 = noir) encodé en base64 — reproduction
# exacte d'une image déjà préparée (ex. le `marvin.raw` validé sur le matériel).
PRINT_RAW_SCHEMA = SERVICE_BASE_SCHEMA.extend({vol.Required("data"): cv.string})

# `image` = PNG/JPEG en base64, mis à l'échelle 576 points par l'intégration.
PRINT_IMAGE_SCHEMA = SERVICE_BASE_SCHEMA.extend(
    {
        vol.Required("image"): cv.string,
        vol.Optional("dither", default=False): cv.boolean,
        vol.Optional("invert", default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configure une imprimante S002."""
    donnees = dict(entry.data)
    options = dict(entry.options)
    adresse = donnees[CONF_ADDRESS].upper()
    imprimante = S002Printer(
        hass,
        address=adresse,
        name=donnees.get(CONF_NAME, "S002"),
        chunk_size=options.get(CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE),
        write_response=options.get(CONF_WRITE_RESPONSE, DEFAULT_WRITE_RESPONSE),
        frame_pause_ms=options.get(CONF_FRAME_PAUSE_MS, DEFAULT_FRAME_PAUSE_MS),
        lines_per_frame=options.get("lines_per_frame", DEFAULT_LINES_PER_FRAME),
        feed_before_mm=options.get(CONF_FEED_BEFORE_MM, 0.0),
        feed_after_mm=options.get(CONF_FEED_AFTER_MM, 0.0),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = imprimante
    _LOGGER.info("S002 %s configurée (%s)", adresse, donnees.get(CONF_NAME, "S002"))
    # Sans ce rechargement, un changement d'options (taille de tranche, mot de passe…)
    # resterait sans effet jusqu'au prochain redémarrage de HA : les réglages sont lus
    # à la construction de S002Printer.
    entry.async_on_unload(entry.add_update_listener(_async_options_modifiees))
    _async_register_services(hass)
    return True


async def _async_options_modifiees(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'entrée quand ses options changent (les réglages sont lus au setup)."""
    _LOGGER.info("S002 : options modifiées → rechargement de l'entrée")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une imprimante."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


def _resoudre(hass: HomeAssistant, call: ServiceCall) -> S002Printer:
    """Trouve l'imprimante visée : par adresse, sinon l'unique configurée."""
    imprimantes: dict[str, S002Printer] = hass.data.get(DOMAIN, {})
    if not imprimantes:
        raise HomeAssistantError("Aucune imprimante S002 configurée")
    adresse = call.data.get(CONF_ADDRESS)
    if adresse:
        adresse = adresse.upper()
        for imprimante in imprimantes.values():
            if imprimante.address == adresse:
                return imprimante
        raise HomeAssistantError(f"Aucune imprimante S002 configurée pour {adresse}")
    if len(imprimantes) > 1:
        raise HomeAssistantError(
            "Plusieurs imprimantes configurées : précisez 'address' dans l'appel du service"
        )
    return next(iter(imprimantes.values()))


def _decoder_base64(valeur: str) -> bytes:
    """Décode une charge base64 (accepte les retours à la ligne et l'absence de padding)."""
    import base64
    import binascii

    propre = "".join(valeur.split())
    try:
        return base64.b64decode(propre + "=" * (-len(propre) % 4), validate=True)
    except (binascii.Error, ValueError) as err:
        raise HomeAssistantError(f"base64 invalide : {err}") from err


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_PRINT_TEST):
        return

    async def _print_test(call: ServiceCall) -> dict:
        resultat = await _resoudre(hass, call).print_test_pattern(
            dry_run=call.data.get("dry_run", False)
        )
        return resultat.as_dict()

    async def _print_text(call: ServiceCall) -> dict:
        # On CONSERVE les lignes vides : elles servent de séparateurs de mise en page
        # (les supprimer collait les paragraphes — défaut constaté). On retire seulement
        # les vides de tête et de queue, qui gaspillent du papier.
        lignes = call.data["text"].splitlines()
        while lignes and not lignes[0].strip():
            lignes.pop(0)
        while lignes and not lignes[-1].strip():
            lignes.pop()
        resultat = await _resoudre(hass, call).print_text(
            lignes,
            scale=call.data.get("scale", 2),
            margin_dots=call.data.get("margin_dots", 16),
            dry_run=call.data.get("dry_run", False),
        )
        return resultat.as_dict()

    async def _feed(call: ServiceCall) -> dict:
        resultat = await _resoudre(hass, call).feed(call.data["mm"])
        return resultat.as_dict()

    async def _print_raw(call: ServiceCall) -> dict:
        raster = _decoder_base64(call.data["data"])
        resultat = await _resoudre(hass, call).print_raster(
            raster, dry_run=call.data.get("dry_run", False)
        )
        return resultat.as_dict()

    async def _print_image(call: ServiceCall) -> dict:
        from . import yk

        try:
            raster = yk.image_to_raster(
                _decoder_base64(call.data["image"]),
                dither=call.data.get("dither", False),
                invert=call.data.get("invert", False),
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Image illisible : {err}") from err
        resultat = await _resoudre(hass, call).print_raster(
            raster, dry_run=call.data.get("dry_run", False)
        )
        return resultat.as_dict()

    async def _diagnose(call: ServiceCall) -> dict:
        return await _resoudre(hass, call).diagnose()

    hass.services.async_register(
        DOMAIN, SERVICE_PRINT_TEST, _print_test,
        schema=SERVICE_BASE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PRINT_TEXT, _print_text,
        schema=PRINT_TEXT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FEED, _feed,
        schema=FEED_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PRINT_RAW, _print_raw,
        schema=PRINT_RAW_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PRINT_IMAGE, _print_image,
        schema=PRINT_IMAGE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DIAGNOSE, _diagnose,
        schema=SERVICE_BASE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    _LOGGER.debug("Services %s enregistrés", DOMAIN)
