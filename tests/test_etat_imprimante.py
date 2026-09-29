"""Le bit de refus d'impression, verrouillé sur les trames réellement capturées.

Ces trois chaînes ne sont pas inventées : ce sont les trames d'état relevées le 29/09/2026 sur le
nœud (champ `brut` du STATUS), pendant l'identification de l'indicateur papier.

    64 | type=ff | cpt | longueur=0008 LE | charge utile 8 o | contrôle | 9b
"""
import pathlib
import sys

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "s002_printer"),
)
from trame import (  # noqa: E402
    BIT_REFUS,
    OCTET_REFUS,
    batterie,
    faut_reappliquer,
    maintien_actif,
    octets,
    parser_status,
    refus_impression,
)

AVEC_PAPIER = "64 ff 0e 08 00 e1 07 03 90 09 03 55 4a ba 5a 34 12 9b"
SANS_PAPIER = "64 ff 0e 08 00 24 08 0b 88 09 03 55 49 ba 5a 34 12 9b"
PAPIER_REMIS = "64 ff 0e 08 00 4f 08 03 90 09 03 55 64 ba 5a 34 12 9b"

echecs = []


def _leve(texte):
    """Vrai si parser_status refuse bien une reponse sans aucune cle=valeur."""
    try:
        parser_status(texte)
    except ValueError:
        return True
    return False


def verifier(quoi, condition, detail=""):
    print(f"  {'OK   ' if condition else 'ECHEC'} {quoi}" + ("" if condition else f"  <- {detail}"))
    if not condition:
        echecs.append(quoi)


print("l'octet surveille est bien la charge utile n°2 de la trame")
o = octets(AVEC_PAPIER)
verifier("18 octets dans la trame", len(o) == 18, str(len(o)))
verifier("debut de trame 0x64", o[0] == "64", o[0])
verifier("fin de trame 0x9b", o[-1] == "9b", o[-1])
verifier("longueur annoncee = 8 (LE16)", o[3] == "08" and o[4] == "00", f"{o[3]} {o[4]}")
verifier(f"octet {OCTET_REFUS} = charge[2] = 0x03 avec papier", o[OCTET_REFUS] == "03", o[OCTET_REFUS])

print()
print("les trois captures donnent le bon etat")
verifier("avec papier  -> pas de refus", refus_impression(AVEC_PAPIER) is False, str(refus_impression(AVEC_PAPIER)))
verifier("sans papier  -> refus", refus_impression(SANS_PAPIER) is True, str(refus_impression(SANS_PAPIER)))
verifier("papier remis -> pas de refus", refus_impression(PAPIER_REMIS) is False, str(refus_impression(PAPIER_REMIS)))

print()
print("le bit surveille est bien 0x08, et lui seul")
verifier(
    "0x08 eteint avec papier, allume sans papier",
    int(o[OCTET_REFUS], 16) // BIT_REFUS % 2 == 0
    and int(octets(SANS_PAPIER)[OCTET_REFUS], 16) // BIT_REFUS % 2 == 1,
)
verifier(
    "l'octet 1 reste a 0x08 apres retour du papier (temon collant, non utilise)",
    octets(PAPIER_REMIS)[6] == "08" and octets(AVEC_PAPIER)[6] == "07",
)

print()
print("robustesse : une trame douteuse ne doit pas inventer un etat")
for douteuse in ("", "64 ff", "64 ff zz", "64 ff 0e 08 00 e1 07 zz 90 09 03 55 4a"):
    verifier(f"{douteuse!r} -> None", refus_impression(douteuse) is None, str(refus_impression(douteuse)))

print()
print("la batterie se lit dans le meme charge (octet 12), regle identique au noeud")
verifier("74 % sur la trame avec papier", batterie(AVEC_PAPIER) == 74, str(batterie(AVEC_PAPIER)))
verifier("100 % sur la trame papier remis", batterie(PAPIER_REMIS) == 100, str(batterie(PAPIER_REMIS)))
verifier("0x00 -> None (jamais recue)", batterie("64 ff 0e 08 00 e1 07 03 90 09 03 55 00") is None)

print()
print("le parseur du STATUS, sur une ligne reelle du noeud")
LIGNE_REELLE = (
    "reset=logiciel uptime=558s etat=pret liaison=tenue mode=manuel maintien=oui batterie=100 "
    "etats=53 partition=ota_1 inactif=13s liberation=120s paquets=11 autorises=29 credits=3 "
    "attentes=0 timeouts=0 ecritures=3 annonces=99 rssi=-16 memoire=105448 refus=2 "
    "trou_max=216989ms trou_max_a=11 creux=6 brut=64 ff 36 08 00 4a 08 03 90 09 03 55 64 5e 5a 34 12 9b"
)
c = parser_status(LIGNE_REELLE)
verifier("la trame brute fait bien 18 octets", len(octets(c["brut"])) == 18, str(len(octets(c["brut"]))))
verifier("elle commence par 64 et finit par 9b", octets(c["brut"])[0] == "64" and octets(c["brut"])[-1] == "9b")
verifier("les espaces de la valeur ne sont pas perdus", c["brut"] == LIGNE_REELLE.split("brut=", 1)[1].strip(),
         repr(c["brut"])[:60])
verifier("le champ suivant l'absorption est intact", c["brut"].count(" ") == 17, str(c["brut"].count(" ")))
verifier("lecture bout en bout : pas de refus sur cette capture", refus_impression(c["brut"]) is False,
         str(refus_impression(c["brut"])))
verifier("lecture bout en bout : batterie 100 %", batterie(c["brut"]) == 100, str(batterie(c["brut"])))
verifier("la reponse complete reste disponible", c["reponse_brute"] == LIGNE_REELLE)
verifier("les entiers restent des entiers", c["creux"] == 6 and c["paquets"] == 11, f"{c['creux']} {c['paquets']}")
verifier("les negatifs aussi (rssi)", c["rssi"] == -16, str(c["rssi"]))
verifier("les valeurs texte aussi", c["partition"] == "ota_1" and c["etat"] == "pret")
verifier("une reponse illisible leve une erreur",
         (lambda: (parser_status("rien du tout"), False)[1] if False else _leve("rien du tout"))(),
         "pas d'erreur")

print()
print("le maintien de liaison : quand faut-il le retablir apres un redemarrage du noeud ?")
MAINTENU = {"maintien": "oui"}
PERDU = {"maintien": "non"}
verifier("aucune intention exprimee -> on ne touche a rien", faut_reappliquer(None, PERDU) is False)
verifier("l'utilisateur l'avait demande et le noeud l'a perdu -> on retablit",
         faut_reappliquer(True, PERDU) is True)
verifier("l'utilisateur l'avait demande et le noeud le fait encore -> rien a faire",
         faut_reappliquer(True, MAINTENU) is False)
verifier("REGLE A SENS UNIQUE : on ne force jamais l'ARRET, meme si le noeud l'a active",
         faut_reappliquer(False, MAINTENU) is False)
verifier("intention d'arreter + noeud deja arrete -> rien a faire",
         faut_reappliquer(False, PERDU) is False)
verifier("le node peut dire oui de trois facons", all(maintien_actif(v) for v in
         ({"maintien": "oui"}, {"maintien": "true"}, {"maintien": "1"})))
verifier("et non autrement", not any(maintien_actif(v) for v in
         ({"maintien": "non"}, {"maintien": ""}, {}, {"maintien": "false"})))

print()
if echecs:
    print(f"  {len(echecs)} ECHEC(S) : " + ", ".join(echecs))
else:
    print("  TOUT PASSE")
sys.exit(1 if echecs else 0)
