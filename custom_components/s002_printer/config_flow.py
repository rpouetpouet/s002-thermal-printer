"""Config flow de l'ORGSTA S002 : une entrée par imprimante.

L'adresse peut être saisie à la main (elle est imprimée sur l'étiquette de l'appareil)
ou préremplie automatiquement quand HA découvre un appareil nommé « S002 ».

⚠️ PIÈGE HA VÉCU (28/09/2026) : un schéma de formulaire ne peut contenir **que** des
validateurs que le sérialiseur de HA sait convertir (`homeassistant.helpers.config_validation.
custom_serializer` + `probatio`). Sont supportés : `str`/`cv.string`, `bool`/`cv.boolean`,
`int`, `float`, et les **selectors**. En revanche `vol.All(vol.Coerce(int), vol.Range(...))`,
`vol.Coerce(...)` et `vol.In(...)` font échouer la sérialisation
(`ValueError: unable to serialize schema`) → **HTTP 500** à la création du flow, sans message
utile côté client. Vérifié en local avec les versions exactes de HA (probatio 0.11.4 +
voluptuous 0.15.2) : voir `tests/test_schema_formulaire.py`.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_NODE_HOST,
    CONF_NODE_PORT,
    CONF_TRANSPORT,
    CONF_CHUNK_SIZE,
    CONF_FEED_AFTER_MM,
    CONF_FEED_BEFORE_MM,
    CONF_FRAME_PAUSE_MS,
    CONF_NAME,
    CONF_WRITE_RESPONSE,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_FEED_AFTER_MM,
    DEFAULT_FRAME_PAUSE_MS,
    DEFAULT_LINES_PER_FRAME,
    DEFAULT_NODE_PORT,
    DEFAULT_TRANSPORT,
    TRANSPORT_NODE,
    TRANSPORT_PROXY,
    DEFAULT_WRITE_RESPONSE,
    FRAME_BUDGET_MS,
    MAX_FRAME_BUDGET_MS,
    MIN_FRAME_BUDGET_MS,
    DOMAIN,
    MAX_LINES_PER_FRAME,
)

# --- Champs numériques : selectors HA (sérialisables + bornes affichées dans l'UI) -------
# Les contraintes sont de toute façon re-bornées dans le code (ble.py / printer.py) :
# le formulaire ne doit jamais être la seule garde.
SEL_PAQUETS = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=20, max=237, step=1, mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="octets",
    )
)
SEL_LIGNES = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1, max=MAX_LINES_PER_FRAME, step=1, mode=selector.NumberSelectorMode.BOX,
    )
)
SEL_PAUSE = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, max=400, step=10, mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="ms",
    )
)
# Budget de durée visé pour une trame : bas = trames courtes (anti-blancs),
# haut = trames longues (moteur continu, espacement régulier).
SEL_BUDGET = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=MIN_FRAME_BUDGET_MS, max=MAX_FRAME_BUDGET_MS, step=50,
        mode=selector.NumberSelectorMode.BOX, unit_of_measurement="ms",
    )
)
SEL_DISTANCE = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, max=100, step=0.5, mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="mm",
    )
)


SEL_TRANSPORT = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(
                value=TRANSPORT_PROXY, label="Bluetooth via proxy ESP32 (historique)"
            ),
            selector.SelectOptionDict(
                value=TRANSPORT_NODE, label="Nœud réseau dédié (TCP) — recommandé"
            ),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)
SEL_PORTE = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
)


def _schema_options(defauts: dict[str, Any]) -> vol.Schema:
    """Réglages de transport, partagés entre la création et les options."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_CHUNK_SIZE, default=defauts.get(CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)
            ): SEL_PAQUETS,
            vol.Optional(
                "lines_per_frame",
                default=defauts.get("lines_per_frame", DEFAULT_LINES_PER_FRAME),
            ): SEL_LIGNES,
            vol.Optional(
                "frame_budget_ms",
                default=defauts.get("frame_budget_ms", FRAME_BUDGET_MS),
            ): SEL_BUDGET,
            vol.Optional(
                CONF_FRAME_PAUSE_MS,
                default=defauts.get(CONF_FRAME_PAUSE_MS, DEFAULT_FRAME_PAUSE_MS),
            ): SEL_PAUSE,
            vol.Optional(
                CONF_FEED_BEFORE_MM, default=defauts.get(CONF_FEED_BEFORE_MM, 0.0)
            ): SEL_DISTANCE,
            vol.Optional(
                CONF_FEED_AFTER_MM,
                  default=defauts.get(CONF_FEED_AFTER_MM, DEFAULT_FEED_AFTER_MM),
            ): SEL_DISTANCE,
            # Transport : le même protocole YK passe soit par le Bluetooth de HA (proxy),
            # soit par un nœud ESP32-C3 dédié joint en TCP sur le réseau local.
            vol.Optional(
                CONF_TRANSPORT, default=defauts.get(CONF_TRANSPORT, DEFAULT_TRANSPORT)
            ): SEL_TRANSPORT,
            # Utilisés seulement si le transport « node » est choisi.
            vol.Optional(CONF_NODE_HOST, default=defauts.get(CONF_NODE_HOST, "")): cv.string,
            vol.Optional(
                CONF_NODE_PORT, default=defauts.get(CONF_NODE_PORT, DEFAULT_NODE_PORT)
            ): SEL_PORTE,
            # bool : sérialisable tel quel par HA (cv.boolean).
            vol.Optional(
                CONF_WRITE_RESPONSE,
                default=defauts.get(CONF_WRITE_RESPONSE, DEFAULT_WRITE_RESPONSE),
            ): cv.boolean,
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
                        CONF_WRITE_RESPONSE: user_input[CONF_WRITE_RESPONSE],
                        CONF_TRANSPORT: user_input[CONF_TRANSPORT],
                        CONF_NODE_HOST: user_input.get(CONF_NODE_HOST, ""),
                        CONF_NODE_PORT: user_input[CONF_NODE_PORT],
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
