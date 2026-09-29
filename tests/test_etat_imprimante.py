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
from trame import BIT_REFUS, OCTET_REFUS, batterie, octets, refus_impression  # noqa: E402

AVEC_PAPIER = "64 ff 0e 08 00 e1 07 03 90 09 03 55 4a ba 5a 34 12 9b"
SANS_PAPIER = "64 ff 0e 08 00 24 08 0b 88 09 03 55 49 ba 5a 34 12 9b"
PAPIER_REMIS = "64 ff 0e 08 00 4f 08 03 90 09 03 55 64 ba 5a 34 12 9b"

echecs = []


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
if echecs:
    print(f"  {len(echecs)} ECHEC(S) : " + ", ".join(echecs))
else:
    print("  TOUT PASSE")
sys.exit(1 if echecs else 0)
