# Bibliothèques tierces auto-hébergées

- `zxing-library.min.js` — [@zxing/library](https://github.com/zxing-js/library) v0.23.0, licence Apache-2.0 (voir `zxing-library.LICENSE`). Bundle UMD téléchargé depuis `https://unpkg.com/@zxing/library@0.23.0/umd/index.min.js` le 2026-07-13, avec l'accord explicite d'Olivier sur cette source précise. Sert à lire les codes-barres EAN à la caméra dans `_product_rows_field.html` — jamais chargé via CDN au runtime, pour ne pas dépendre d'un service externe à chaque utilisation.
