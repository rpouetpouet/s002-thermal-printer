# Police embarquée

`DejaVuSansCondensed-Bold.ttf` — DejaVu Sans Condensed Bold, utilisée pour rasteriser le texte
imprimé par `s002_printer.print_text`.

**Pourquoi embarquée** : l'intégration tourne sur la machine de Home Assistant, dont on ne
maîtrise pas les polices installées. La police par défaut de Pillow, utilisée avant, avait des
fûts d'un seul point (texte trop clair) et **ne savait pas écrire les accents** — ils sortaient en
carré vide.

**Pourquoi condensée** : à l'échelle d'impression 2, la version condensée donne 5,0 % de points
noirs contre 5,2 % pour la version normale, mais **40 caractères par ligne au lieu de 36** — sur
48,8 mm de large, la capacité compte.

**Origine** : paquet `dejavu-fonts-ttf` (<https://www.npmjs.com/package/dejavu-fonts-ttf>),
récupéré via <https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf/ttf/DejaVuSansCondensed-Bold.ttf>.

**Licence** : Bitstream Vera Fonts Copyright, avec les modifications DejaVu dans le domaine public —
voir `LICENSE-DejaVu.txt`, à conserver avec le fichier de police.
