"""Police du texte imprimé : grasse, embarquée, et accents corrects (régression v0.2.4).

Le texte sortait trop clair : il était dessiné avec la police par défaut de Pillow, dont les fûts
font un seul point de large. Cette police ne sait en plus **pas écrire les accents** — ils
sortaient en carré vide (« Abricot, ⃞t⃞ »), constaté sur une planche de rendu avant correction.

Ce test verrouille les trois propriétés qui comptent :
  1. c'est bien la police EMBARQUÉE qui est utilisée (et pas un repli silencieux) ;
  2. le texte est plus noir, mesuré en part de points noirs ;
  3. les caractères accentués ne sont pas des carrés vides.
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent
SOURCE = RACINE / "custom_components" / "s002_printer"


def _paquet_temporaire(avec_police: bool = True) -> None:
    """Reconstruit un paquet importable, comme le fait Home Assistant."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    pkg = tmp / "s002_printer"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for nom in ("const.py", "yk.py"):
        shutil.copy(SOURCE / nom, pkg / nom)
    if avec_police:
        shutil.copytree(SOURCE / "fonts", pkg / "fonts")
    sys.path.insert(0, str(tmp))
    for module in ("s002_printer", "s002_printer.const", "s002_printer.yk"):
        sys.modules.pop(module, None)


_paquet_temporaire()
from s002_printer import yk  # noqa: E402  (après la construction du paquet)


def _encre(raster: bytes) -> float:
    """Part de points noirs, en pourcentage."""
    return 100 * sum(bin(octet).count("1") for octet in raster) / (len(raster) * 8)


class TestPoliceTexte(unittest.TestCase):
    def test_police_embarquee_utilisee(self):
        police = yk._police_texte(22)
        nom = police.getname()[0]
        self.assertIn(
            "DejaVu", nom,
            f"la police embarquée n'est pas utilisée (obtenu : {nom!r}) — un repli silencieux "
            "donnerait un texte plus clair sans le dire",
        )

    def test_texte_plus_noir_que_la_police_par_defaut(self):
        raster = yk.text_raster(["Contraste du texte"], scale=2)
        self.assertGreaterEqual(
            _encre(raster), 4.5,
            "le texte doit sortir plus noir qu'avec la police par défaut de Pillow (3,1 % mesuré) "
            "et avec la police condensée retenue (5,0 % mesuré)",
        )

    def test_texte_plus_noir_a_la_taille_par_defaut(self):
        """La taille par défaut du service est 3 (2 auparavant) : elle doit sortir nettement noire."""
        raster = yk.text_raster(["Liste de courses", "Abricot, été, à côté"], scale=3)
        self.assertGreaterEqual(
            _encre(raster), 9.0,
            "à l'échelle 3 le texte doit dépasser 9 % de points noirs (10,9 % mesuré sur planche)",
        )

    def test_seuil_de_binarisation_suffisant(self):
        """Le seuil retenu (SEUIL_ENCRE) doit rester dans la zone vérifiée sur planche."""
        self.assertEqual(yk.SEUIL_ENCRE, 170)

    def test_inversion_toujours_correcte(self):
        """Le chemin d'inversion a été réécrit (dessin en niveaux de gris) : il doit inverser."""
        normal = yk.text_raster(["Texte"], scale=3)
        inverse = yk.text_raster(["Texte"], scale=3, invert=True)
        self.assertGreater(_encre(inverse), 50.0, "invert=True doit donner un fond noir majoritaire")
        self.assertLess(_encre(normal), 50.0)

    def test_accents_pas_des_carres_vides(self):
        """Un caractère absent d'une police est rendu en carré (« tofu »).

        On compare donc chaque accent au rendu d'un code réservé, forcément absent : si les deux
        bitmaps sont identiques, l'accent est manquant.
        """
        from PIL import Image, ImageDraw

        def bitmap(police, caractere: str) -> bytes:
            """Rendu du caractère seul, pour comparer deux dessins."""
            img = Image.new("L", (60, 40), 255)
            ImageDraw.Draw(img).text((4, 4), caractere, font=police, fill=0)
            return img.tobytes()

        police = yk._police_texte(22)
        tofu = bitmap(police, "\ue000")          # code réservé : forcément absent
        self.assertNotEqual(bitmap(police, "e"), tofu, "témoin : « e » doit être présent")
        for accent in "éèêàçùôîûÉÀÇœ":
            with self.subTest(accent=accent):
                self.assertNotEqual(
                    bitmap(police, accent), tofu,
                    f"« {accent} » est rendu en carré vide : la police ne contient pas le caractère",
                )

    def test_raster_garde_la_forme_d_impression(self):
        raster = yk.text_raster(["Liste de courses", "Lait, pain, abricots"], scale=2)
        self.assertEqual(len(raster) % yk.BYTES_PER_LINE, 0, "le raster doit contenir des lignes entières")
        self.assertGreater(len(raster) // yk.BYTES_PER_LINE, 0)
        # Invariant de largeur : une ligne fait 72 octets, soit 576 points imprimables.
        self.assertEqual(yk.BYTES_PER_LINE * 8, yk.PRINT_WIDTH_DOTS)
        self.assertGreater(_encre(raster), 1.0)

    def test_repli_si_la_police_manque(self):
        """Sans le dossier `fonts/`, une police est quand même renvoyée (jamais d'exception)."""
        _paquet_temporaire(avec_police=False)
        from s002_printer import yk as yk_sans_police

        police = yk_sans_police._police_texte(22)
        self.assertIsNotNone(police.getmask("A").getbbox())
        ras = yk_sans_police.text_raster(["Texte de repli"], scale=2)
        self.assertGreater(_encre(ras), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
