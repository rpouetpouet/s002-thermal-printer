"""Config flow de l'ORGSTA S002 : une entrée par imprimante.

L'adresse peut être saisie à la main (elle est imprimée sur l'étiquette de l'appareil)
ou préremplie automatiquement quand HA découvre un appareil nommé « S002 ».
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

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

REGEX_MAC = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CHUNK_SIZE, default=DEFAULT_CHUNK_SIZE): vol.All(
            vol.Coerce(int), vol.Range(min=20, max=237)
        ),
        vol.Optional("lines_per_frame", default=MAX_LINES_PER_FRAME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_LINES_PER_FRAME)
        ),
        vol.Optional(CONF_FRAME_PAUSE_MS, default=DEFAULT_FRAME_PAUSE_MS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=400)
        ),
        vol.Optional(CONF_FEED_BEFORE_MM, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional(CONF_FEED_AFTER_MM, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
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
            adresse = user_input[CONF_ADDRESS].strip().upper()
            if not REGEX_MAC.match(adresse):
                erreurs[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(adresse)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or f"S002 {adresse[-5:]}",
                    data={CONF_ADDRESS: adresse, CONF_NAME: user_input.get(CONF_NAME, "S002")},
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
                vol.Required(
                    CONF_ADDRESS, default=self._adresse_decouverte or ""
                ): cv_string,
                vol.Optional(CONF_NAME, default="S002"): cv_string,
                **OPTIONS_SCHEMA.schema,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=erreurs,
            description_placeholders={"defaults": "576 points / 40 lignes par trame"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return S002OptionsFlow(config_entry)


class S002OptionsFlow(OptionsFlow):
    """Réglages fins : c'est ici qu'on ajuste le débit pour un proxy BLE.

    `self.config_entry` est fourni par le framework (ne pas l'assigner).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = dict(self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CHUNK_SIZE, default=options.get(CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)
                ): vol.All(vol.Coerce(int), vol.Range(min=20, max=237)),
                vol.Optional(
                    "lines_per_frame", default=options.get("lines_per_frame", MAX_LINES_PER_FRAME)
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_LINES_PER_FRAME)),
                vol.Optional(
                    CONF_FRAME_PAUSE_MS,
                    default=options.get(CONF_FRAME_PAUSE_MS, DEFAULT_FRAME_PAUSE_MS),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=400)),
                vol.Optional(
                    CONF_FEED_BEFORE_MM, default=options.get(CONF_FEED_BEFORE_MM, 0.0)
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                vol.Optional(
                    CONF_FEED_AFTER_MM, default=options.get(CONF_FEED_AFTER_MM, 0.0)
                ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def cv_string(value: Any) -> str:
    """Petit validateur texte (évite d'importer tout config_validation ici)."""
    return str(value).strip()
