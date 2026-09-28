#!/usr/bin/env python3
"""Test de non-régression : le schéma du formulaire doit rester sérialisable par HA.

Pourquoi ce test existe : un schéma de config flow contenant un validateur que HA ne sait
pas sérialiser fait planter la **création du flow** avec un simple `HTTP 500` sans message
utile. Le diagnostic a coûté plusieurs allers-retours — d'où ce garde-fou.

Le test reproduit le comportement réel de Home Assistant avec **ses versions exactes**
(`probatio==0.11.4`, `voluptuous==0.15.2`, cf. `homeassistant/package_constraints.txt`) :

  1. quels validateurs passent et lesquels échouent (le piège, chiffré) ;
  2. que `config_flow.py` n'utilise QUE des validateurs de la liste blanche.

Exécution :
    python3 -m venv .venv && .venv/bin/pip install "probatio==0.11.4" "voluptuous==0.15.2"
    .venv/bin/python tests/test_schema_formulaire.py
"""
from __future__ import annotations

import pathlib
import re
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FLOW = RACINE / "custom_components" / "s002_printer" / "config_flow.py"

echecs: list[str] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    print(("  OK    " if condition else "  ECHEC ") + nom + ("" if condition else f" — {detail}"))
    if not condition:
        echecs.append(nom)


# ---------------------------------------------------------------------------------------
print("=== 1. Comportement du sérialiseur de HA (versions exactes) ===")
try:
    import voluptuous as vol
    from probatio.codecs._shared import UNSUPPORTED
    from probatio.codecs.fields import to_field_list
except ImportError as err:
    print(f"  (dépendances absentes : {err}) — installer probatio==0.11.4 et voluptuous==0.15.2")
    sys.exit(0)

print(f"  voluptuous {vol.__version__}")


def faux_serialiseur_ha(node):
    """Imitation de homeassistant.helpers.config_validation.custom_serializer."""
    if node is str:
        return {"type": "string"}
    if node is bool:
        return {"type": "boolean"}
    return UNSUPPORTED


SUPPORTES = {
    "str": str,
    "bool": bool,
    "int": int,
    "float": float,
}
REFUSES = {
    "vol.All(vol.Coerce(int), vol.Range(...))": vol.All(vol.Coerce(int), vol.Range(min=1, max=9)),
    "vol.Coerce(int)": vol.Coerce(int),
    "vol.In([...])": vol.In(["a", "b"]),
    "fonction Python maison": (lambda v: str(v)),
}


def serialiser(valeur):
    return to_field_list({"champ": valeur}, custom_serializer=faux_serialiseur_ha)


for nom, valeur in SUPPORTES.items():
    try:
        champs = serialiser(valeur)
        verifier(f"{nom} sérialisable", bool(champs and champs[0].get("type")), str(champs))
    except Exception as err:  # noqa: BLE001
        verifier(f"{nom} sérialisable", False, f"{type(err).__name__}: {err}")

for nom, valeur in REFUSES.items():
    try:
        serialiser(valeur)
        verifier(f"{nom} est bien refusé (piège documenté)", False, "aucune exception levée")
    except ValueError as err:
        verifier(f"{nom} est bien refusé (piège documenté)", "unable to serialize" in str(err), str(err))
    except Exception as err:  # noqa: BLE001
        verifier(f"{nom} est bien refusé", False, f"exception inattendue {type(err).__name__}: {err}")

# ---------------------------------------------------------------------------------------
print("\n=== 2. config_flow.py n'utilise que des validateurs sûrs ===")
import ast  # noqa: E402

source = CONFIG_FLOW.read_text()
# On analyse le CODE seul : les docstrings et commentaires citent volontairement les
# motifs interdits pour documenter le piège, ils ne doivent pas déclencher le test.
arbre = ast.parse(source)
for noeud in ast.walk(arbre):
    if isinstance(noeud, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        corps = noeud.body
        if (
            corps
            and isinstance(corps[0], ast.Expr)
            and isinstance(corps[0].value, ast.Constant)
            and isinstance(corps[0].value.value, str)
        ):
            corps.pop(0)
code = ast.unparse(arbre)

interdits = {
    "vol.All(": "non sérialisable",
    "vol.Coerce(": "non sérialisable",
    "vol.In(": "non sérialisable",
    "vol.Range(": "non sérialisable",
}
for motif, pourquoi in interdits.items():
    verifier(f"aucun `{motif}` dans le schéma ({pourquoi})", motif not in code)

comptes = len(re.findall(r"selector\.(Number|Text|Boolean|Select)Selector\(", source))
verifier("les champs numériques utilisent des selectors HA", comptes >= 4, f"{comptes} selector(s)")

print("\n" + "=" * 70)
if echecs:
    print(f"ECHECS ({len(echecs)}) : " + ", ".join(echecs))
    sys.exit(1)
print("TOUS LES TESTS PASSENT — schéma de formulaire sérialisable par Home Assistant")
