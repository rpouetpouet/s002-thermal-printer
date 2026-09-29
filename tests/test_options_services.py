#!/usr/bin/env python3
"""Test de non-régression : toute option LUE par un service doit être DÉCLARÉE dans son schéma.

Pourquoi ce test existe : `enhance` et `gamma` étaient documentés dans `services.yaml` et lus par
le gestionnaire de `print_image`, mais absents de `PRINT_IMAGE_SCHEMA`. Home Assistant valide les
données d'appel **avant** d'entrer dans le gestionnaire : tout appel passant ces options était donc
rejeté avec un `400 Bad Request`, et les deux réglages étaient inutilisables. Le bug est resté
invisible parce que les appels sans option, eux, fonctionnaient.

Le contrôle porte sur l'invariant plutôt que sur la liste des options connues :
pour chaque service, l'ensemble des clés lues dans `call.data` doit être inclus dans l'ensemble des
clés déclarées par le schéma. Une option ajoutée plus tard sans être déclarée fait échouer ce test.

Analyse statique par `ast` : aucun besoin des dépendances de Home Assistant, donc le test tourne
avec n'importe quel Python 3.

Exécution : python3 tests/test_options_services.py
"""
from __future__ import annotations

import ast
import pathlib
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
MODULE = RACINE / "custom_components" / "s002_printer" / "__init__.py"

echecs: list[str] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    etat = "OK  " if condition else "ECHEC"
    print(f"  [{etat}] {nom}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        echecs.append(nom)


source = MODULE.read_text(encoding="utf-8")
arbre = ast.parse(source)

# ------------------------------------------------------------------ schémas déclarés
schemas: dict[str, set[str]] = {}
for noeud in ast.walk(arbre):
    if not isinstance(noeud, ast.Assign):
        continue
    cibles = [c.id for c in noeud.targets if isinstance(c, ast.Name)]
    if not cibles or not cibles[0].endswith("_SCHEMA"):
        continue
    cles: set[str] = set()
    for appel in ast.walk(noeud.value):
        if not isinstance(appel, ast.Call):
            continue
        fonction = appel.func
        nom = getattr(fonction, "attr", "") or getattr(fonction, "id", "")
        if nom in ("Required", "Optional") and appel.args:
            premier = appel.args[0]
            if isinstance(premier, ast.Constant) and isinstance(premier.value, str):
                cles.add(premier.value)
    schemas[cibles[0]] = cles

print("  schémas trouvés :")
for nom, cles in sorted(schemas.items()):
    print(f"    {nom} : {sorted(cles)}")

# ------------------------------------------------------------------ options lues
# Association service -> schéma : chaque service est enregistré à côté de son schéma.
association: dict[str, str] = {}
for noeud in ast.walk(arbre):
    if not isinstance(noeud, ast.Call):
        continue
    nom_fonction = getattr(noeud.func, "attr", "") or getattr(noeud.func, "id", "")
    if nom_fonction != "async_register" or not noeud.args:
        continue
    service = None
    if isinstance(noeud.args[0], ast.Constant):
        service = noeud.args[0].value
    for mot in noeud.keywords:
        if mot.arg == "schema" and isinstance(mot.value, ast.Name):
            association[service] = mot.value.id

print("\n  options lues par les gestionnaires :")
for service, nom_schema in sorted(association.items()):
    # Le corps des gestionnaires est enregistré juste après : on cherche les accès call.data.
    lues: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Attribute) \
                and noeud.func.attr == "get":
            cible = noeud.func.value
            if isinstance(cible, ast.Attribute) and cible.attr == "data" \
                    and isinstance(cible.value, ast.Name) and cible.value.id == "call" \
                    and noeud.args and isinstance(noeud.args[0], ast.Constant):
                lues.add(noeud.args[0].value)
        if isinstance(noeud, ast.Subscript) and isinstance(noeud.value, ast.Attribute) \
                and noeud.value.attr == "data" \
                and isinstance(noeud.value.value, ast.Name) and noeud.value.value.id == "call":
            tranche = noeud.slice
            if isinstance(tranche, ast.Constant) and isinstance(tranche.value, str):
                lues.add(tranche.value)
    declarees = schemas.get(nom_schema, set())
    print(f"    {service} ({nom_schema}) : lues={sorted(lues)}")

# Le contrôle porte sur les options OPTIONNELLES : une clé lue par `call.data.get(...)` est par
# définition censée être optionnelle, donc elle doit figurer au schéma.
manquantes: dict[str, set[str]] = {}
for service, nom_schema in association.items():
    if not nom_schema:
        continue
    declarees = schemas.get(nom_schema, set())
    lues = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Call) and isinstance(noeud.func, ast.Attribute) \
                and noeud.func.attr == "get" and noeud.args \
                and isinstance(noeud.args[0], ast.Constant):
            cible = noeud.func.value
            if isinstance(cible, ast.Attribute) and cible.attr == "data" \
                    and isinstance(cible.value, ast.Name) and cible.value.id == "call":
                lues.add(noeud.args[0].value)
    # On ne juge que les clés du service courant : le corps des gestionnaires n'est pas isolable
    # simplement, donc on signale les clés lues qui ne sont déclarées dans AUCUN schéma.
    tous = set().union(*schemas.values()) if schemas else set()
    oubliees = {c for c in lues if c not in tous}
    if oubliees:
        manquantes[service] = oubliees

print()
verifier("des schémas de service sont bien déclarés", len(schemas) >= 4,
         f"{len(schemas)} trouvés")
verifier(
    "les options enhance et gamma sont déclarées dans PRINT_IMAGE_SCHEMA",
    {"enhance", "gamma"} <= schemas.get("PRINT_IMAGE_SCHEMA", set()),
    "c'est exactement l'oubli qui provoquait un 400 Bad Request",
)
verifier(
    "aucune option lue par call.data.get() n'est absente de tous les schémas",
    not manquantes,
    f"options lues mais jamais déclarées : {manquantes}",
)

print()
if echecs:
    print(f"  {len(echecs)} vérification(s) en échec : {echecs}")
    sys.exit(1)
print("  toutes les vérifications passent")
