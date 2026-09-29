#!/usr/bin/env python3
"""Genere les images de marque de l'integration (dossier `brand/`).

Specifications officielles (depuis Home Assistant 2026.3, une integration personnalisee peut
embarquer ses images ; source : depot home-assistant/brands, section « Image specification ») :

    - PNG, transparence preferee, rogne au plus juste (pas de marge vide), pas d'image
      de marque Home Assistant ;
    - icone : carree 1:1, 256x256 (normal) et 512x512 (hDPI) ;
    - logo : paysage, cote le plus court entre 128 et 256 px (normal), entre 256 et 512 px
      pour la version hDPI.

Usage :
    python3 tools/make_brand.py [dossier_de_sortie]
    # defaut : custom_components/s002_printer/brand/

Le motif est dessine en grand (4x) puis reduit avec LANCZOS : c'est ce qui donne des bords
nets, un trace au trait n'etant jamais anticrene par lui-meme.
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

# Palette : un corps ardoise qui tient sur fond clair comme sur fond sombre, du papier
# blanc casse, et un accent turquoise (la teinte de l'appareil).
ARDOISE = (71, 89, 107, 255)
ARDOISE_SOMBRE = (38, 49, 59, 255)
PAPIER = (255, 255, 255, 255)
PAPIER_TRAIT = (46, 125, 143, 255)
ACCENT = (46, 125, 143, 255)
VOYANT = (61, 214, 140, 255)
GRIS_TEXTE = (108, 122, 137, 255)

RAPPORT = 4  # facteur de suréchantillonnage


def _corps_imprimante(d: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int) -> None:
    """Dessine un corps d'imprimante a tickets : rouleau de papier + corps + fente."""
    largeur = x1 - x0
    rayon = int(largeur * 0.09)

    # --- rouleau de papier (avec un bord dechire en haut, comme un ticket coupe) ---
    marge_papier = int(largeur * 0.30)
    px0, px1 = x0 + marge_papier, x1 - marge_papier
    dent = int((px1 - px0) / 9)
    sommet = y0 + int((y1 - y0) * 0.10)
    base_papier = y0 + int((y1 - y0) * 0.52)
    dentes = [(px0 + i * dent, sommet - (dent if i % 2 == 0 else 0)) for i in range(10)]
    d.polygon(
        [*dentes, (px1, base_papier), (px0, base_papier)],
        fill=PAPIER,
        outline=ARDOISE,
        width=max(2, largeur // 90),
    )

    # --- contenu imprime sur le ticket : des lignes de longueurs variables ---
    trait = max(3, largeur // 70)
    lignes = (0.92, 0.62, 0.80, 0.45)
    hauteur_ligne = int((base_papier - sommet) * 0.13)
    y = sommet + int((base_papier - sommet) * 0.22)
    for largeur_relative in lignes:
        largeur_trait = int((px1 - px0 - 2 * trait) * largeur_relative)
        d.rounded_rectangle(
            [px0 + trait, y, px0 + trait + largeur_trait, y + hauteur_ligne],
            radius=hauteur_ligne // 2,
            fill=PAPIER_TRAIT,
        )
        y += int(hauteur_ligne * 2.0)

    # --- corps de l'appareil ---
    corps_haut = y0 + int((y1 - y0) * 0.46)
    d.rounded_rectangle([x0, corps_haut, x1, y1], radius=rayon, fill=ARDOISE)

    # --- fente de sortie du papier, la ou le ticket entre dans le corps ---
    fente = int(largeur * 0.34)
    d.rounded_rectangle(
        [x0 + fente, corps_haut - int(largeur * 0.02), x1 - fente, corps_haut + int(largeur * 0.045)],
        radius=int(largeur * 0.02),
        fill=ARDOISE_SOMBRE,
    )

    # --- bouton d'avance (avance le papier) et voyant d'etat ---
    cote_bouton = int(largeur * 0.17)
    bouton_x = x0 + (largeur - cote_bouton) // 2
    bouton_y = corps_haut + int((y1 - corps_haut) * 0.30)
    d.rounded_rectangle(
        [bouton_x, bouton_y, bouton_x + cote_bouton, bouton_y + cote_bouton],
        radius=int(cote_bouton * 0.28),
        fill=ACCENT,
    )
    barre = int(cote_bouton * 0.16)
    d.rounded_rectangle(
        [
            bouton_x + (cote_bouton - barre) // 2,
            bouton_y + int(cote_bouton * 0.30),
            bouton_x + (cote_bouton + barre) // 2,
            bouton_y + int(cote_bouton * 0.70),
        ],
        radius=barre // 2,
        fill=PAPIER,
    )
    rayon_voyant = int(largeur * 0.045)
    voyant_x = x1 - int(largeur * 0.19)
    voyant_y = bouton_y + cote_bouton // 2
    d.ellipse(
        [voyant_x - rayon_voyant, voyant_y - rayon_voyant, voyant_x + rayon_voyant, voyant_y + rayon_voyant],
        fill=VOYANT,
    )


def _police(taille: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Police grasse du systeme, avec repli sur celle livree par Pillow."""
    for chemin in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if pathlib.Path(chemin).exists():
            return ImageFont.truetype(chemin, taille)
    return ImageFont.load_default(size=taille)


def _police_ajustee(
    d: ImageDraw.ImageDraw,
    texte: str,
    largeur_max: int,
    taille_max: int,
    taille_min: int = 12,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, int, int]:
    """Reduit la police jusqu'a ce que le texte tienne dans la largeur disponible.

    Sans cette mesure, un texte plus long que prevu sort de la toile et se retrouve coupe
    au bord : c'est exactement ce qui est arrive au premier jet du mot-symbole.
    Renvoie (police, largeur, hauteur) du texte une fois ajuste.
    """
    taille = taille_max
    while taille > taille_min:
        police = _police(taille)
        boite = d.textbbox((0, 0), texte, font=police)
        if boite[2] - boite[0] <= largeur_max:
            return police, boite[2] - boite[0], boite[3] - boite[1]
        taille -= max(1, taille_max // 40)
    police = _police(taille_min)
    boite = d.textbbox((0, 0), texte, font=police)
    return police, boite[2] - boite[0], boite[3] - boite[1]


def icone(cote: int) -> Image.Image:
    """Icone carree : 1:1, rognee au plus juste (marge ~3 %)."""
    grand = cote * RAPPORT
    image = Image.new("RGBA", (grand, grand), (0, 0, 0, 0))
    marge = int(grand * 0.03)
    _corps_imprimante(ImageDraw.Draw(image), marge, marge, grand - marge, grand - marge)
    return image.resize((cote, cote), Image.Resampling.LANCZOS)


def logo(largeur: int, hauteur: int) -> Image.Image:
    """Logo paysage : l'appareil a gauche, le nom du produit a droite."""
    grand_l, grand_h = largeur * RAPPORT, hauteur * RAPPORT
    image = Image.new("RGBA", (grand_l, grand_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    marge = int(grand_h * 0.05)
    cote_motif = grand_h - 2 * marge
    _corps_imprimante(d, marge, marge, marge + cote_motif, grand_h - marge)

    # Le motif occupe la gauche ; le mot-symbole prend exactement la place restante.
    x_texte = marge + cote_motif + int(grand_h * 0.14)
    largeur_texte = grand_l - x_texte - marge
    # Les deux lignes forment un bloc centre verticalement : on mesure chaque ligne
    # (boite reelle, offsets compris) et on pose l'ensemble, au lieu d'empiler des y a la main
    # — c'est cet empilage qui avait colle le sous-titre sous les chiffres.
    police_nom, _, _ = _police_ajustee(d, "S002", largeur_texte, int(grand_h * 0.56))
    police_sous, _, _ = _police_ajustee(
        d, "imprimante thermique", largeur_texte, int(grand_h * 0.15), taille_min=14
    )
    boite_nom = d.textbbox((0, 0), "S002", font=police_nom)
    boite_sous = d.textbbox((0, 0), "imprimante thermique", font=police_sous)
    haut_nom = boite_nom[3] - boite_nom[1]
    haut_sous = boite_sous[3] - boite_sous[1]
    interligne = int(grand_h * 0.10)
    y_bloc = (grand_h - (haut_nom + interligne + haut_sous)) // 2
    d.text((x_texte - boite_nom[0], y_bloc - boite_nom[1]), "S002", font=police_nom, fill=ARDOISE)
    d.text(
        (x_texte - boite_sous[0], y_bloc + haut_nom + interligne - boite_sous[1]),
        "imprimante thermique",
        font=police_sous,
        fill=GRIS_TEXTE,
    )
    return image.resize((largeur, hauteur), Image.Resampling.LANCZOS)


def main() -> int:
    defaut = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "s002_printer" / "brand"
    sortie = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else defaut
    sortie.mkdir(parents=True, exist_ok=True)

    fichiers = {
        "icon.png": icone(256),
        "icon@2x.png": icone(512),
        "logo.png": logo(512, 256),
        "logo@2x.png": logo(1024, 512),
    }
    for nom, image in fichiers.items():
        chemin = sortie / nom
        # optimize + pas d'entrelacement : PNG compressé au mieux (specs : lossless prefere)
        image.save(chemin, format="PNG", optimize=True)
        print(f"  {chemin}  {image.width}x{image.height}  {chemin.stat().st_size} o")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
