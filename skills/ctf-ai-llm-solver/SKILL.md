---
name: ctf-ai-llm-solver
description: Solveur CTF AI/LLM. Charge la methodology core et applique les quick wins et outils spécifiques aux systèmes ML/LLM (chatbots, RAG, classifiers, fichiers modèles, agents). Pour prompt injection, jailbreak, adversarial examples, model inversion, audit de safetensors/pickle, RAG poisoning, alignment bypass.
category: ai_llm
---

# CTF AI/LLM Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement ce qui est propre aux challenges IA/LLM.

## Graphiti en priorité

**Au step Recon**, avant tout: interroger Graphiti sur le groupe `ecole2600_securite_ia` (6 cours complets: surface d'attaque, reverse/introspection, manifolds, LLM, attention/alignement, jailbreak) plus `ctf_writeups`. Si un pattern, un nom de challenge ou une technique est déjà documenté, tu gagnes 90% du travail. Read only.

```
search_nodes(query="<mot-clé du challenge>", group_ids=["ecole2600_securite_ia", "ctf_writeups"])
search_memory_facts(query="<technique soupçonnée>", group_ids=["ecole2600_securite_ia", "ctf_writeups"])
```

## Classifier la cible (surface mapping)

1. **Chatbot / API LLM** → prompt injection, jailbreak, system prompt leak, context extraction.
2. **RAG** → indirect injection via document ingéré, vector DB poisoning, embedding inversion.
3. **Classifier** (text/image/audio) → adversarial examples, transfer attack.
4. **Fichier modèle fourni** (`.pt`, `.safetensors`, `.onnx`, `.h5`) → audit + extraction + inversion.
5. **Agent LLM avec outils** → injection indirecte, confused deputy, side-channel via tool call.

Le détail des techniques par famille vit dans `reference/attacks.md` — charger uniquement si Graphiti ne couvre pas déjà le cas.

## Quick Wins (ordre d'essai)

1. `ignore previous instructions` + variantes (ordre, casse, reformulation, traduction).
2. Demander le system prompt direct, puis en markdown, puis dans une autre langue, puis en code.
3. Base64 / leetspeak / rot13 / fiction / académique pour bypass filtre.
4. DAN / persona classique avant toute ingénierie élaborée.
5. Fichier modèle: `file` + `strings` + listing des tenseurs. Chercher `latent_payload`, `hidden.N.weight`, texte brut encodé dans un embedding.
6. RAG: injection via un document que le système va retrieve.

## Anti-patterns à éviter

- Ne **pas** commencer par GCG / abliteration sur un black-box qui céderait à `ignore previous instructions`.
- Ne **pas** charger un pickle inconnu (`torch.load` sur `.pt`) dans un process sensible. Sandbox ou `pickletools` d'abord.
- Ne **pas** fabriquer un flag. Si l'attaque n'a pas produit de sortie vérifiable, `flag=null` + `next_step` actionnable.

## Tools spécifiques

- `python` + `transformers`, `safetensors`, `torch` (sandbox)
- `pickletools`, `fickling` pour audit pickle
- `whisper` (baseline ASR), `ffmpeg` pour manipuler audio/vidéo
- `openai` / `anthropic` SDK pour APIs publiques
- `numpy`, `torch.nn.functional.cosine_similarity` pour décoder des embeddings

## Notes ecole2600

Graphiti confirme que `ctfia.ecole2600.com` a déjà exposé du shell hors du chatbot principal. Toujours vérifier: endpoint admin, fichier téléchargeable, prompt visible dans le HTML, header custom. La plateforme cache régulièrement des indices hors dialogue LLM.

Challenges connus (consulter Graphiti pour les writeups exacts): **Executive Command**, **OpenSubs Whisper**, **secret_vector.safetensors** (TinyLlama-1.1B → `FLAG{L4TENT_P1R4T3_CH4T}`).
