#!/usr/bin/env python3
"""Vérifie que l'encodeur de l'intégration produit EXACTEMENT les octets validés sur le matériel.

Référence = la logique du script `s002_dos_a_dos.py` qui a réellement imprimé « MARVIN »
(5 trames de 40 lignes, dos à dos) sur l'imprimante le 28/09/2026.

Exécution : /home/batman/venvs/printer/bin/python tests/test_yk.py
"""
from __future__ import annotations

import pathlib
import shutil
import struct
import sys
import tempfile

RACINE = pathlib.Path(__file__).resolve().parent.parent
SRC = RACINE / "custom_components" / "s002_printer"

# --- on importe le module comme un paquet (comme le fera Home Assistant) --------------
tmp = pathlib.Path(tempfile.mkdtemp())
pkg = tmp / "s002_printer"
pkg.mkdir()
(pkg / "__init__.py").write_text("")
for nom in ("const.py", "yk.py"):
    shutil.copy(SRC / nom, pkg / nom)
sys.path.insert(0, str(tmp))
sys.modules.pop("s002_printer", None)
sys.modules.pop("s002_printer.const", None)
sys.modules.pop("s002_printer.yk", None)

from s002_printer import yk  # noqa: E402
from s002_printer import const as C  # noqa: E402

echecs: list[str] = []


def verifier(nom: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {nom}")
    else:
        print(f"  ECHEC {nom} {detail}")
        echecs.append(nom)


print("=== 1. Trame élémentaire (octet par octet)")
trame = yk.build_frame(0x80, b"\x01", 0)
attendu = bytes([0x64, 0x80, 0x00, 0x01, 0x00, 0x01, 0, 0, 0, 0, 0x9B])
verifier("frame token = 11 octets attendus", trame == attendu, f"{trame.hex(' ')} != {attendu.hex(' ')}")

trame = yk.build_frame(0x0F, struct.pack("<H", 576), 1)
attendu = bytes([0x64, 0x0F, 0x01, 0x02, 0x00, 0x40, 0x02, 0, 0, 0, 0, 0x9B])
verifier("frame largeur 576 -> 40 02 en LE", trame == attendu, trame.hex(" "))

trame = yk.frame_feed(5.0, 2)
verifier("frame avance 5 mm -> 50 unités", trame[5:7] == struct.pack("<H", 50), trame.hex(" "))
# Échelle validée à la règle sur papier : 150 unités = 15,0 mm mesurés.
verifier("frame avance 15 mm -> 150 unités (échelle validée sur papier)",
         yk.frame_feed(15.0, 3)[5:7] == struct.pack("<H", 150), yk.frame_feed(15.0, 3).hex(" "))

verifier("compteur modulo 64", yk.build_frame(0x00, b"", 64)[2] == 0)

print("\n=== 2. Découpage en trames : comparaison avec le script ayant imprimé MARVIN")
raw = (pathlib.Path(__file__).resolve().parent / "fixtures" / "marvin.raw").read_bytes()
verifier("raster de référence = 14 400 o (200 lignes)", len(raw) == 14400, str(len(raw)))

# référence : reproduction exacte de la logique du script Pi validé
LIGNE_T = 40
lignes = [raw[i : i + 72].ljust(72, b"\x00") for i in range(0, len(raw), 72)]
ref_tranches = [b"".join(lignes[i : i + LIGNE_T]) for i in range(0, len(lignes), LIGNE_T)]

obtenu = list(yk.iter_image_frames(raw, LIGNE_T, start_counter=3))   # 3 = après token (1) + largeur (2)
verifier("5 trames produites", len(obtenu) == 5, f"{len(obtenu)}")
verifier("5 trames de référence", len(ref_tranches) == 5, f"{len(ref_tranches)}")

identiques = 0
for (trame, ctr), ref in zip(obtenu, ref_tranches):
    attendu = bytes([0x64, 0x00, ctr & 0x3F]) + len(ref).to_bytes(2, "little") + ref + b"\x00\x00\x00\x00\x9b"
    if trame == attendu:
        identiques += 1
verifier("trames identiques au script validé (octet pour octet)", identiques == 5, f"{identiques}/5")

compteurs = [c for _, c in obtenu]
verifier("compteurs incrémentaux 3..7 (chronologie réelle)",
         compteurs == [3, 4, 5, 6, 7], str(compteurs))
verifier("chaque trame = 40 lignes pleines (2 890 o au total)",
         all(len(t[0]) == 2880 + 10 for t in obtenu),
         str([len(t[0]) for t in obtenu]))
verifier("AUCUN payload ne dépasse la limite de 2 880 o",
         all(len(t[0][5:-5]) <= 2880 for t in obtenu),
         str([len(t[0][5:-5]) for t in obtenu]))

reconstitue = b"".join(t[0][5:-5] for t in obtenu)
verifier("raster reconstitué à l'identique", reconstitue == raw, f"{len(reconstitue)} vs {len(raw)}")

print("\n=== 2ter. Séquence complète comparée au script validé (compteurs inclus)")
# Le script qui a imprimé « MARVIN » fait : token (ctr 1) → 400 ms → largeur (ctr 2)
# → 400 ms → tranches (ctr 3, 4, 5, ...). On reproduit cette séquence ici.
sequence = [(0x80, 1), (0x0F, 2)] + [
    (0x00, c & 0x3F) for c in range(3, 3 + len(ref_tranches))
]
obtenue = [(0x80, 1), (0x0F, 2)] + [(0x00, c) for _, c in obtenu]
verifier("5 trames d'image après token+largeur", len(obtenue) == 7, f"{len(obtenue)} trames au total")
verifier("séquence (type, compteur) identique au script validé",
         obtenue == sequence, f"{obtenue} != {sequence}")
verifier("token = compteur 1 et largeur = compteur 2",
         obtenue[0] == (0x80, 1) and obtenue[1] == (0x0F, 2) and yk.frame_token(1)[2] == 1,
         f"token={obtenue[0]}, largeur={obtenue[1]}, octet compteur={yk.frame_token(1)[2]}")

print("\n=== 3. Contraintes du protocole")
try:
    list(yk.iter_image_frames(raw, 41, 0))
    verifier("41 lignes par trame refusées par l'encodeur", False, "aucune exception")
except ValueError:
    verifier("41 lignes par trame refusées par l'encodeur", True)

try:
    list(yk.iter_image_frames(raw, 0, 0))
    verifier("0 ligne par trame refusée", False, "aucune exception")
except ValueError:
    verifier("0 ligne par trame refusée", True)

raster_impair = b"\xff" * 100      # 100 o : pas un multiple de 72
frames = list(yk.iter_image_frames(raster_impair, 40, 0))
payload = frames[0][0][5:-5]
verifier("raster non aligné complété à une ligne entière", len(payload) == 144, str(len(payload)))

print("\n=== 4. Motifs et géométrie")
motif = yk.test_pattern_raster()
verifier("test_pattern multiple de 72 o", len(motif) % 72 == 0, f"{len(motif)}")
verifier("test_pattern = 168 lignes (repères début/fin inclus)", len(motif) // 72 == 168, f"{len(motif)//72}")
frames_motif = list(yk.iter_image_frames(motif, 40, 0))
verifier("test_pattern tient en 5 trames", len(frames_motif) == 5, str(len(frames_motif)))
verifier("chaque trame du motif <= 2 880 o",
         all(len(f[0][5:-5]) <= 2880 for f in frames_motif),
         str([len(f[0][5:-5]) for f in frames_motif]))

petit = yk.stripes_raster(3, 24)
verifier("stripes(3) multiple de 72 o", len(petit) % 72 == 0, f"{len(petit)}")
verifier("stripes(3) = 24 lignes (2 mm, 1 trame)", len(petit) // 72 == 24, f"{len(petit)//72}")

stats = yk.raster_stats(motif)
verifier("hauteur du motif de test ≈ 14,2 mm", 13.5 <= stats["height_mm"] <= 15.0, str(stats))
verifier("largeur déclarée 48,8 mm", 48.0 <= stats["width_mm"] <= 49.5, str(stats))

print("\n=== 5. Rendu de texte (Pillow)")
try:
    import PIL  # noqa: F401
    texte = yk.text_raster(["MARVIN", "S002 - test"], scale=2)
    verifier("raster texte multiple de 72 o", len(texte) % 72 == 0, str(len(texte)))
    verifier("raster texte contient du noir", any(b for b in texte))
    frames_txt = list(yk.iter_image_frames(texte, 40, 0))
    verifier("chaque trame de texte <= 2 880 o",
             all(len(f[0][5:-5]) <= 2880 for f in frames_txt),
             str([len(f[0][5:-5]) for f in frames_txt]))
    print(f"       -> {len(texte)//72} lignes ({len(texte)//72/11.81:.1f} mm), "
          f"{len(texte)} o, {len(frames_txt)} trame(s)")
except ImportError:
    print("  (Pillow absent : test de rendu ignoré)")

print("\n=== 6. Découpage en paquets BLE")
p = yk.chunks(b"\xab" * 2458, 200)
verifier("paquets de 200 o", [len(x) for x in p] == [200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 58], str([len(x) for x in p]))
verifier("contenu préservé", b"".join(p) == b"\xab" * 2458)
# Régression : les sélecteurs numériques de HA renvoient des flottants (200.0) et
# range() les refuse — TypeError vécu en production le 28/09.
p_float = yk.chunks(b"\xab" * 450, 200.0)
verifier("taille de paquet flottante acceptée (selectors HA)", [len(x) for x in p_float] == [200, 200, 50],
         str([len(x) for x in p_float]))

print("\n=== 7. Préparation des photos (variante A) et garde-fou image binaire")
try:
    import io as _io

    from PIL import Image, ImageDraw

    def _png(img) -> bytes:
        tampon = _io.BytesIO()
        img.save(tampon, format="PNG")
        return tampon.getvalue()

    # (a) image « photo » peu contrastée : un dégradé doux de 100 à 160 en niveaux de gris
    photo = Image.new("L", (720, 480))
    px = photo.load()
    for y in range(480):
        for x in range(720):
            px[x, y] = 100 + int(60 * (x / 719))

    sans = yk.image_to_raster(_png(photo), dither=True, enhance=False)
    avec = yk.image_to_raster(_png(photo), dither=True, enhance=True)
    verifier("enhance change le rendu d'une photo peu contrastée", sans != avec)
    verifier("enhance + tramage donne plus d'encre (niveaux étalés)",
              sum(bin(b).count("1") for b in avec) > sum(bin(b).count("1") for b in sans),
              f"{sum(bin(b).count('1') for b in avec)} vs {sum(bin(b).count('1') for b in sans)}")
    verifier("raster photo = largeur 576 points (72 o par ligne)",
              len(avec) % 72 == 0 and len(avec) // 72 > 0, str(len(avec)))

    # (b) image déjà binaire (QR code / tracé) : le tramage doit être IGNORÉ
    bilevel = Image.new("L", (720, 480), 255)
    dessin = ImageDraw.Draw(bilevel)
    for i in range(0, 720, 40):
        dessin.rectangle([i, 0, i + 20, 479], fill=0)
    png_bilevel = _png(bilevel)
    verifier("détection : image binaire reconnue",
              yk._est_binaire(Image.open(_io.BytesIO(png_bilevel)).convert("L")))
    verifier("détection : photo NON classée binaire",
             not yk._est_binaire(Image.open(_io.BytesIO(_png(photo))).convert("L")))
    a = yk.image_to_raster(png_bilevel, dither=True, enhance=False)
    b = yk.image_to_raster(png_bilevel, dither=False, enhance=False)
    verifier("image binaire : tramage demandé == seuil franc (QR protégé)", a == b)
    c = yk.image_to_raster(png_bilevel, dither=True, enhance=True)
    verifier("image binaire : le garde-fou tient aussi avec enhance", c == a)

    # (c) photo : tramage et seuil franc doivent bien différer, sinon le garde-fou est trop large
    d = yk.image_to_raster(_png(photo), dither=False, enhance=True)
    verifier("photo : tramage != seuil franc (garde-fou non déclenché à tort)", d != avec)

    # (d) correction de tons : compense le gain de point du papier thermique.
    # 0,85 = réglage retenu par Rich après comparaison de 4 densités sur papier.
    def _encre(r: bytes) -> int:
        return sum(bin(octet).count("1") for octet in r)

    g100 = yk.image_to_raster(_png(photo), dither=True, enhance=True, gamma=1.0)
    g085 = yk.image_to_raster(_png(photo), dither=True, enhance=True, gamma=0.85)
    g065 = yk.image_to_raster(_png(photo), dither=True, enhance=True, gamma=0.65)
    verifier("gamma 0,85 éclaircit par rapport à 1,00 (réglage retenu)",
             _encre(g085) < _encre(g100), f"{_encre(g085)} vs {_encre(g100)}")
    verifier("gamma 0,65 éclaircit encore (le réglage va dans le bon sens)",
             _encre(g065) < _encre(g085), f"{_encre(g065)} vs {_encre(g085)}")
    verifier("gamma n'altère pas la géométrie", len(g085) == len(g100))
except ImportError:
    print("  (Pillow absent : tests de préparation ignorés)")

print("\n" + "=" * 70)
if echecs:
    print(f"ECHECS ({len(echecs)}) : " + ", ".join(echecs))
    sys.exit(1)
print("TOUS LES TESTS PASSENT — encodeur identique à celui validé sur le matériel")
