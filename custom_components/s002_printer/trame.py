"""Lecture de la trame d'état de l'imprimante (sans dépendance à Home Assistant).

La trame d'état fait 18 octets et le nœud la publie en clair dans le champ `brut` du STATUS :

    64 | type | compteur | longueur LE16 | charge utile (8 o) | contrôle (4 o) | 9b

soit, en indices 0-based : en-tête sur 0..4, charge utile sur 5..12, contrôle sur 13..16, fin en 17.
La charge utile porte notamment :

- `charge[7]` (octet 12) : batterie en pourcent ;
- `charge[2]` (octet 7) : bit `0x08` = l'imprimante a REFUSÉ la dernière impression tentée.
"""

#: Octet de la charge utile qui porte le bit de refus.
OCTET_REFUS = 7
#: Bit allumé quand l'imprimante refuse d'imprimer.
BIT_REFUS = 0x08


def octets(trame: str) -> list[str]:
    """Découpe la trame hexadécimale en octets, en ignorant les espaces multiples."""
    return str(trame or "").split()


def refus_impression(trame: str) -> bool | None:
    """Vrai si le bit de refus est allumé, Faux s'il est éteint, None si la trame est illisible.

    Mesuré le 29/09/2026 sur trois captures réelles :
        avec papier : charge[2] = 0x03  -> éteint
        sans papier : charge[2] = 0x0b  -> allumé (0x08 ajouté), après une tentative d'impression
        papier remis : charge[2] = 0x03 -> éteint
    """
    o = octets(trame)
    if len(o) <= OCTET_REFUS:
        return None
    try:
        return bool(int(o[OCTET_REFUS], 16) & BIT_REFUS)
    except ValueError:
        return None


def batterie(trame: str) -> int | None:
    """Batterie en pourcent, ou None si la trame est illisible (même règle que le nœud)."""
    o = octets(trame)
    if len(o) <= 12:
        return None
    try:
        valeur = int(o[12], 16)
    except ValueError:
        return None
    return valeur if 0 < valeur <= 100 else None
