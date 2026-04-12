---
name: ctf-mobile-solver
description: Solveur CTF Mobile. Charge la methodology core et applique les quick wins, pivots et outils spécifiques APK/IPA, stockage local, composants exportés, backends mobiles.
---

# CTF Mobile Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques mobile.

## Quick Wins
- Secrets, flags, URLs, tokens hardcodés dans smali/java/ressources
- Composants exportés, deeplinks, intent filters, providers ou activities mal protégés
- `shared_prefs`, SQLite, fichiers internes, logs, cache, assets
- Vérifications root/debug/pinning triviales à patcher
- Endpoints mobiles présents dans le code mais pas dans l'UI visible

## High-Value Pivots
- **Statique d'abord.** `apktool`, `jadx`, `strings`, `rg` sur le code décompilé.
- Si l'app parle à une API, reconstruire l'appel en CLI (`curl`/`httpie`) avant toute émulation.
- Instrumenter (Frida / objection) uniquement après que la statique ait confirmé la surface.
- iOS: `plutil`, inspection bundle, entitlements, plist.
- Au step Research: Graphiti + web sur la lib native ou le framework (Flutter, React Native, Cordova, etc.) si l'app n'est pas en Java/Kotlin standard.

## Tools spécifiques
`aapt`, `apktool`, `jadx`, `sqlite3`, `adb`, `frida`, `objection`, `plutil`, `class-dump`.
