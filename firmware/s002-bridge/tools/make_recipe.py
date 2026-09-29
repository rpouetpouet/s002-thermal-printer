#!/usr/bin/env python3
"""Genere le raster 1 bit de la recette, embarque dans le firmware du noeud C3.

Le rendu passe par `yk.text_raster()` de l'integration HA : le papier du test long est donc
strictement ce que HA enverrait, sans re-implementation approximative.

Usage :
    python3 tools/make_recipe.py [fichier_texte] > main/recette.bin

Echelle 4 = celle validee sur papier le 28/09/2026 (recette ~94 mm, 1115 lignes).
"""
from __future__ import annotations

import pathlib
import sys
import unicodedata

RACINE_DEPOT = pathlib.Path(__file__).resolve().parents[3]

# On charge `yk.py` SANS passer par `custom_components/s002_printer/__init__.py`, qui importe
# voluptuous (dependance Home Assistant absente ici). yk.py n'a besoin que de la stdlib et de
# son propre const.py : on fabrique donc un paquet minimal a la main.
import importlib.util  # noqa: E402
import types  # noqa: E402

_PAQUET = RACINE_DEPOT / "custom_components" / "s002_printer"
_pkg = types.ModuleType("s002_printer")
_pkg.__path__ = [str(_PAQUET)]
sys.modules["s002_printer"] = _pkg
_spec = importlib.util.spec_from_file_location("s002_printer.yk", _PAQUET / "yk.py")
yk = importlib.util.module_from_spec(_spec)
sys.modules["s002_printer.yk"] = yk
_spec.loader.exec_module(yk)

# La police bitmap de Pillow ne contient AUCUN caractere accentue (verifie : carres vides).
# On translittere avant rendu, sinon le papier sort avec des trous.
TRANSLITTERATION = {
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE",
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "—": "-", "–": "-", "…": "...", "°": " deg", "€": "EUR", "×": "x",
}


def sans_accents(texte: str) -> str:
    for source, cible in TRANSLITTERATION.items():
        texte = texte.replace(source, cible)
    return "".join(c for c in unicodedata.normalize("NFKD", texte) if not unicodedata.combining(c))


def main() -> int:
    source = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("/tmp/crepes_lignes.txt")
    texte = source.read_text(encoding="utf-8")
    lignes = [sans_accents(l).rstrip() for l in texte.splitlines()]
    while lignes and not lignes[-1].strip():
        lignes.pop()

    raster = yk.text_raster(lignes, scale=4, padding_dots=8)

    nb_lignes = len(raster) // 72
    hauteur_mm = nb_lignes / 11.81
    octets = len(raster)

    if len(raster) % 72:
        print(f"ERREUR : raster non multiple de 72 o ({len(raster)})", file=sys.stderr)
        return 1

    sortie = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else (
        pathlib.Path(__file__).resolve().parents[1] / "main" / "recette.bin"
    )
    sortie.write_bytes(raster)
    print(f"  ecrit             : {sortie} ({sortie.stat().st_size} o)", file=sys.stderr)

    print(f"  source            : {source}", file=sys.stderr)
    print(f"  lignes de texte   : {len(lignes)}", file=sys.stderr)
    print(f"  raster            : {nb_lignes} lignes de 72 o = {octets} octets", file=sys.stderr)
    print(f"  hauteur papier    : {hauteur_mm:.1f} mm  ({hauteur_mm / 20 * 1000:.0f} ms a 20 mm/s)", file=sys.stderr)
    print(f"  debit requis      : {octets / (hauteur_mm / 20):.0f} o/s", file=sys.stderr)
    print(f"  tranches de 8 l.  : {(nb_lignes + 7) // 8}", file=sys.stderr)
    print(f"  ecritures de 200 o: {(octets + 199) // 200}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
