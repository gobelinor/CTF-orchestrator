---
name: ctf-stego-solver
description: Solveur CTF Stéganographie. Charge la methodology core et applique les quick wins, pivots et outils spécifiques à l'extraction de données cachées dans images, audio, vidéo, texte.
---

# CTF Stego Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques stego.

## Quick Wins
- Metadata + strings + `file` + `binwalk` en premier. 30% des stego se résolvent ici.
- Appended data (ZIP/TAR derrière une image), magic bytes secondaires
- LSB image (canaux couleur, alpha, palette, bitplanes)
- Audio: spectrogramme, symboles Morse, DTMF, SSTV
- Mot de passe: dérivé du contexte du challenge, JAMAIS de dictionnaire d'abord

## High-Value Pivots
- Comparer structure réelle du fichier vs structure attendue (`pngcheck`, `ffprobe`).
- Extraire une couche à la fois, pas tout en parallèle.
- Si un payload sort, le traiter comme un nouveau challenge indépendant.
- Au step Research: Graphiti → `ctf_writeups` + web pour technique nommée (StegHide, OpenStego, F5, outils custom).

## Tools spécifiques
`exiftool`, `binwalk`, `zsteg`, `steghide`, `stegseek`, `sox`, `Pillow` / `wave` pour scripts ciblés, `ffmpeg` pour vidéo.
