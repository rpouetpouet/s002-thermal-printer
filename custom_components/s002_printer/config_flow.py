"""Config flow de l'ORGSTA S002 : une entrée par imprimante.

L'adresse peut être saisie à la main (elle est imprimée sur l'étiquette de l'appareil)
ou préremplie automatiquement quand HA découvre un appareil nommé « S002 ».

⚠️ PIÈGE HA VÉCU (28/09/2026) : un schéma de formulaire ne peut contenir **que** des
validateurs sérialisables (`cv.string`, `cv.boolean`, `vol.All(vol.Coerce(int), vol.Range(...))`,
…). Utiliser une simple fonction Python maison comme type de champ fait échouer la
sérialisation du formulaire côté Home Assistant (`probatio.codecs.fields.to_field_list`),
ce qui se traduit par un **HTTP 500** à la création du flow — sans message explicite côté
client. Les validateurs de HA, jamais les siens.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADDRESS,
    CONF_CHUNK_SIZE,
    CONF_FEED_AFTER_MM,
    CONF_FEED_BEFORE_MM,
    CONF_FRAME_PAUSE_MS,
    CONF_NAME,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_FRAME_PAUSE_MS,
    DOMAIN,
    MAX_LINES_PER_FRAME,
)

# Uniquement des validateurs que HA sait sérialiser vers le frontend.
NOMBRE_PAQUETS = vol.All(vol.Coerce(int), vol.Range(min=20, max=237))
NOMBRE_LIGNES = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_LINES_PER_FRAME))
PAUSE_MS = vol.All(vol.Coerce(int), vol.Range(min=0, max=400))
DISTANCE_MM = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))


def _schema_options(defauts: dict[str, Any]) -> vol.Schema:
    """Réglages de transport, partagés entre la création et les options."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_CHUNK_SIZE, default=defauts.get(CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)
            ): NOMBRE_PAQUETS,
            vol.Optional(
                "lines_per_frame",
                default=defauts.get("lines_per_frame", MAX_LINES_PER_FRAME),
            ): NOMBRE_LIGNES,
            vol.Optional(
                CONF_FRAME_PAUSE_MS,
                default=defauts.get(CONF_FRAME_PAUSE_MS, DEFAULT_FRAME_PAUSE_MS),
            ): PAUSE_MS,
            vol.Optional(
                CONF_FEED_BEFORE_MM, default=defauts.get(CONF_FEED_BEFORE_MM, 0.0)
            ): DISTANCE_MM,
            vol.Optional(
                CONF_FEED_AFTER_MM, default=defauts.get(CONF_FEED_AFTER_MM, 0.0)
            ): DISTANCE_MM,
        }
    )


class S002ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Assistant de configuration."""

    VERSION = 1

    def __init__(self) -> None:
        self._adresse_decouverte: str | None = None

    async def async_step_bluetooth(self, discovery_info: Any) -> FlowResult:
        """Imprimante découverte automatiquement par HA."""
        adresse = getattr(discovery_info, "address", None)
        if adresse:
            self._adresse_decouverte = adresse.upper()
            await self.async_set_unique_id(adresse.upper())
            self._abort_if_unique_id_configured()
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Saisie de l'adresse et des réglages."""
        erreurs: dict[str, str] = {}
        if user_input is not None:
            adresse = str(user_input[CONF_ADDRESS]).strip().upper()
            if not _adresse_valide(adresse):
                erreurs[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(adresse)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=str(user_input.get(CONF_NAME) or f"S002 {adresse[-5:]}"),
                    data={
                        CONF_ADDRESS: adresse,
                        CONF_NAME: str(user_input.get(CONF_NAME) or "S002"),
                    },
                    options={
                        CONF_CHUNK_SIZE: user_input[CONF_CHUNK_SIZE],
                        "lines_per_frame": user_input["lines_per_frame"],
                        CONF_FRAME_PAUSE_MS: user_input[CONF_FRAME_PAUSE_MS],
                        CONF_FEED_BEFORE_MM: user_input[CONF_FEED_BEFORE_MM],
                        CONF_FEED_AFTER_MM: user_input[CONF_FEED_AFTER_MM],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default=self._adresse_decouverte or ""): cv.string,
                vol.Optional(CONF_NAME, default="S002"): cv.string,
                **_schema_options({}).schema,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=erreurs,
            description_placeholders={"geometry": "576 points (48,8 mm) / 40 lignes par trame"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return S002OptionsFlow()


class S002OptionsFlow(OptionsFlow):
    """Réglages fins : c'est ici qu'on ajuste le débit pour un proxy BLE.

    `self.config_entry` est fourni par le framework (ne pas l'assigner).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init", data_schema=_schema_options(dict(self.config_entry.options))
        )


def _adresse_valide(adresse: str) -> bool:
    """Adresse MAC au format AA:BB:CC:DD:EE:FF."""
    morceaux = adresse.split(":")
    return len(morceaux) == 6 and all(
        len(m) == 2 and all(c in "0123456789ABCDEF" for c in m) for m in morceaux
    )
