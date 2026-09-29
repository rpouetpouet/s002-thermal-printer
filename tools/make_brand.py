#!/usr/bin/env python3
"""Genere les images de marque de l'integration (dossier `brand/`) a partir de la photo.

Specifications officielles (depuis Home Assistant 2026.3 une integration personnalisee peut
embarquer ses images ; source : depot home-assistant/brands, « Image specification ») :

    - PNG, transparence preferee, rogne au plus juste, pas d'image de marque Home Assistant ;
    - icone : carree 1:1, 256x256 (normal) et 512x512 (hDPI) ;
    - logo : paysage, cote le plus court entre 128 et 256 px (normal), entre 256 et 512 px (hDPI).

Usage :
    python3 tools/make_brand.py [dossier_de_sortie]
    # defaut : custom_components/s002_printer/brand/

Source : `tools/brand-source/s002-printer.jpg` (visuel de l'appareil, retenu pour
l'identification du produit). Le fond est retire automatiquement :

1. le fond du visuel est du blanc pur alors que la carrosserie descend plus bas : un remplissage
   par diffusion (flood fill) depuis les quatre coins, a faible tolerance, detoure l'appareil
   sans entamer sa coque ;
2. le visuel contient un autre objet dans le coin inferieur droit, separe de l'appareil par des
   pixels plus sombres : une tolerance fixe ne peut pas l'isoler. On garde donc la **plus grande
   region connexe** — l'appareil — et tout le reste est jete, ce qui elimine du meme coup les
   poussières laissees par la compression JPEG ;
3. l'image est rognee, centree dans un carre (marge ~7 %) puis reduite en LANCZOS.
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SOURCE = pathlib.Path(__file__).resolve().parent / "brand-source" / "s002-printer.jpg"

# Le visuel montre l'appareil a gauche et un second objet a droite : ce cadrage ne garde que
# la zone de l'appareil.
CADRAGE = (0, 0, 842, 962)
TOLERANCE_FOND = 6      # ecart minimal au blanc pur : en dessous, la carrosserie serait mangee
MARGE = 1.04            # marge autour du sujet (specs : rogne au plus juste)
FACTEUR = 2             # composition du logo en 2x, puis reduction

ARDOISE = (71, 89, 107, 255)


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
    d: ImageDraw.ImageDraw, texte: str, largeur_max: int, taille_max: int, taille_min: int = 12
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, int]:
    """Reduit la police jusqu'a ce que le texte tienne dans la largeur disponible.

    Sans cette mesure, un texte plus long que prevu sort de la toile et se retrouve coupe au
    bord : c'est arrive au premier jet du mot-symbole.
    """
    taille = taille_max
    while taille > taille_min:
        police = _police(taille)
        boite = d.textbbox((0, 0), texte, font=police)
        if boite[2] - boite[0] <= largeur_max:
            return police, int(boite[3] - boite[1])
        taille -= max(1, taille_max // 40)
    police = _police(taille_min)
    return police, int(d.textbbox((0, 0), texte, font=police)[3])


def _garder_plus_grande_region(alpha: Image.Image) -> Image.Image:
    """Ne conserve que la plus grande tache connexe de `alpha` : l'appareil.

    Les regions sont etiquetees par remplissage par diffusion sur le masque lui-meme (chaque
    region recoit un numero), puis leurs surfaces sont lues d'un seul coup dans l'histogramme.
    """
    masque = alpha.copy()
    marqueur = 1
    for y in range(0, masque.size[1], 3):
        for x in range(0, masque.size[0], 3):
            if masque.getpixel((x, y)) == 255:
                if marqueur > 250:      # garde-fou : au-dela on ne sait plus etiqueter
                    break
                ImageDraw.floodfill(masque, (x, y), marqueur, thresh=0)
                marqueur += 1
    surfaces = masque.histogram()
    rang = sorted((surfaces[i] for i in range(1, marqueur)), reverse=True)
    garde = max(range(1, marqueur), key=lambda i: surfaces[i])
    print(f"  regions connexes : {marqueur - 1} | appareil = {surfaces[garde]} px, "
          f"suivante = {rang[1] if len(rang) > 1 else 0} px (jete)")
    return masque.point(lambda v: 255 if v == garde else 0)


def detourer() -> Image.Image:
    """Renvoie l'appareil detoure (RGBA), rogne et centre dans un carre."""
    im = Image.open(SOURCE).convert("RGB").crop(CADRAGE)
    magenta = (255, 0, 255)
    rempli = im.copy()
    for coin in ((0, 0), (im.size[0] - 1, 0), (0, im.size[1] - 1), (im.size[0] - 1, im.size[1] - 1)):
        ImageDraw.floodfill(rempli, coin, magenta, thresh=TOLERANCE_FOND)
    ecart = ImageChops.difference(rempli, Image.new("RGB", im.size, magenta)).convert("L")
    alpha = ecart.point(lambda v: 0 if v == 0 else 255).filter(ImageFilter.MedianFilter(3))
    alpha = _garder_plus_grande_region(alpha)

    sujet = im.copy()
    sujet.putalpha(alpha)
    sujet = sujet.crop(alpha.getbbox())
    print(f"  appareil rogne : {sujet.size[0]}x{sujet.size[1]}")

    cote = int(max(sujet.size) * MARGE)
    carre = Image.new("RGBA", (cote, cote), (0, 0, 0, 0))
    carre.paste(sujet, ((cote - sujet.size[0]) // 2, (cote - sujet.size[1]) // 2), sujet)
    return carre


def icone(appareil: Image.Image, cote: int) -> Image.Image:
    """Icone carree, rognee au plus juste."""
    return appareil.resize((cote, cote), Image.Resampling.LANCZOS)


def logo(appareil: Image.Image, largeur: int, hauteur: int) -> Image.Image:
    """Logo paysage : l'appareil a gauche, le mot-symbole a droite."""
    grand_l, grand_h = largeur * FACTEUR, hauteur * FACTEUR
    image = Image.new("RGBA", (grand_l, grand_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    marge = int(grand_h * 0.05)
    cote_appareil = grand_h - 2 * marge
    vignette = appareil.resize((cote_appareil, cote_appareil), Image.Resampling.LANCZOS)
    image.paste(vignette, (marge, marge), vignette)

    # Le mot-symbole prend exactement la place restante, et il est mesure : jamais tronque.
    x_texte = marge + cote_appareil + int(grand_h * 0.12)
    largeur_texte = grand_l - x_texte - marge
    police, haut = _police_ajustee(d, "S002", largeur_texte, int(grand_h * 0.58))
    boite = d.textbbox((0, 0), "S002", font=police)
    d.text((x_texte - boite[0], (grand_h - haut) // 2 - boite[1]), "S002", font=police, fill=ARDOISE)
    return image.resize((largeur, hauteur), Image.Resampling.LANCZOS)


def main() -> int:
    defaut = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "s002_printer" / "brand"
    sortie = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else defaut
    sortie.mkdir(parents=True, exist_ok=True)

    appareil = detourer()
    fichiers = {
        "icon.png": icone(appareil, 256),
        "icon@2x.png": icone(appareil, 512),
        "logo.png": logo(appareil, 512, 256),
        "logo@2x.png": logo(appareil, 1024, 512),
    }
    for nom, image in fichiers.items():
        chemin = sortie / nom
        image.save(chemin, format="PNG", optimize=True)
        print(f"  {chemin}  {image.width}x{image.height}  {chemin.stat().st_size} o")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
