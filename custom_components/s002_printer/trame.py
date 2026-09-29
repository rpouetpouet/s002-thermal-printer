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


def parser_status(texte: str) -> dict:
    """Transforme la réponse du nœud en dictionnaire clé/valeur.

    Deux pièges, tous deux rencontrés le 29/09/2026 :

    - une valeur peut contenir des ESPACES (la trame d'état, par exemple : `brut=64 ff 0e …`).
      Un mot sans `=` n'est donc pas à jeter : c'est la suite de la valeur précédente ;
    - la clé `brut` désignait auparavant la réponse ENTIÈRE, attribuée en fin de boucle : elle
      écrasait la trame. La réponse complète est désormais sous `reponse_brute`.

    Sans ces deux corrections, `brut` se réduisait à `64` puis à la ligne entière, et toute
    lecture d'octet tombait au mauvais endroit.
    """
    champs: dict[str, object] = {}
    derniere_cle: str | None = None
    for morceau in str(texte or "").split():
        if "=" not in morceau:
            if derniere_cle:
                champs[derniere_cle] = f"{champs[derniere_cle]} {morceau}"
            continue
        derniere_cle, valeur = morceau.split("=", 1)
        try:
            champs[derniere_cle] = int(valeur)
        except ValueError:
            champs[derniere_cle] = valeur
    if not champs:
        raise ValueError(f"réponse de STATUT illisible : {texte!r}")
    champs["reponse_brute"] = str(texte or "").strip()
    return champs


def maintien_actif(etat_noeud: dict) -> bool:
    """Vrai si le nœud dit qu'il garde la liaison."""
    return str(etat_noeud.get("maintien", "")).lower() in ("oui", "true", "1")


def faut_reappliquer(voulu: bool | None, etat_noeud: dict) -> bool:
    """Vrai s'il faut renvoyer `MAINTENIR 1` au nœud.

    Un redémarrage du nœud (OTA, coupure, BROWNOUT) le remet en libération automatique et efface le
    maintien : l'intention de l'utilisateur est donc réappliquée après coup.

    La règle est volontairement À SENS UNIQUE : on ne force jamais l'ARRÊT. D'une part l'arrêt est
    l'état par défaut (rien à rétablir), d'autre part forcer un arrêt viendrait contredire une
    activation faite ailleurs — et le bouton « Libérer le Bluetooth », qui rend la liaison à un
    téléphone, est justement le cas où l'intention devient « ne plus maintenir ».
    """
    return voulu is True and not maintien_actif(etat_noeud)


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
