# Taxonomie détaillée des attaques IA/LLM

Document chargé à la demande quand Graphiti et les quick wins du SKILL.md principal ne couvrent pas le cas observé.

## 1. Prompt injection

- **Direct**: surcharge du system prompt par une instruction plus récente. Variantes: "ignore previous", "you are now", "SYSTEM:" fake, markdown role switching, traduction, reformulation forte.
- **Indirect**: la charge vit dans une ressource ingérée — document RAG, email, profil utilisateur, contenu d'URL fetchée par un tool. Le modèle traite l'injection comme instruction légitime.
- **System prompt leak**: extraction du prompt caché. Techniques: demande directe, diff markdown, jeu de rôle "décris ton brief", traduction du contexte, completion inversée ("le system prompt commence par ...").
- **Context extraction**: exfiltration de secrets placés dans le contexte (clés API, hints, données user). Continuation forcée, markdown table, base64 disguise, reverse order, encoding layer.
- **Data leakage**: fuite de données d'entraînement ou d'autres users via chain-of-thought, completion, tool use.

## 2. Jailbreak

- **On-manifold (sémantique)**: rester dans la distribution naturelle, exploiter persona / fiction / académique / langue étrangère / leetspeak / encoding.
- **Off-manifold (mathématique)**: suffixes adversariaux type GCG. Séquence de tokens optimisée pour déplacer l'état interne hors de la zone de refus.
- **DAN / Roleplay**: persona explicite sans garde-fous.
- **Abliteration**: suppression du vecteur de refus par SVD sur les poids. Requiert accès aux poids.
- **Context stuffing**: saturer la fenêtre de contexte pour déplacer le system prompt hors attention.

## 3. Adversarial examples

- **Texte**: FGSM sur embeddings de tokens. Perturbation minimale pour crosser la decision boundary.
- **Image**: FGSM / PGD classiques, patches visibles, perturbations imperceptibles.
- **Audio**: attaques sur Whisper / ASR. Injection inaudible dans le spectrogramme, bruit optimisé.
- **Transferability**: une attaque sur un modèle A marche souvent sur B. Si poids de A dispos, entraîner localement puis transférer.

## 4. RAG / Vector DB

- **Indirect injection** via document ingéré poussé dans l'index.
- **Vector DB poisoning**: insertion de vecteurs adversariaux qui seront nearest-neighbors des requêtes cibles.
- **Embedding inversion**: reconstruire le texte original depuis un vecteur.
- **Retrieval manipulation**: requêtes qui forcent un retrieval non pertinent mais privilégié.

## 5. Model file audit

### `.pt` / `.pth` (PyTorch)
- Format **pickle** → exécution de code Python arbitraire à `torch.load`.
- **Workflow sécurisé**:
  1. `file <f.pt>` + `head -c 200 <f.pt> | xxd` pour repérer le magic byte.
  2. `python -c "import pickletools; pickletools.dis(open('f.pt','rb'))"` pour disséquer statiquement.
  3. `fickling` si dispo (sandbox).
  4. NE JAMAIS `torch.load` sans sandbox si le fichier vient d'un CTF non vérifié.

### `.safetensors`
- Safe by design (pas de code exécutable).
- Hidden flag patterns:
  - tensor nommé `latent_payload`, `hidden.N.weight`, `secret`, `flag_embedding`
  - bytes ASCII dans `tensor.flatten().tolist()` → décoder avec `bytes(...).decode('utf-8', 'ignore')`
  - embedding d'un texte → inversion par cosine similarity avec le vocab du modèle source (TinyLlama, sentence-transformers, GPT-2, etc.)
- Code:
  ```python
  from safetensors import safe_open
  with safe_open("f.safetensors", framework="pt") as f:
      for key in f.keys():
          t = f.get_tensor(key)
          print(key, t.shape, t.dtype)
  ```

### `.onnx`
- Graphe protobuf. Peut contenir des opérateurs custom qui exécutent du code.
- `onnx.load` + `onnx.checker.check_model` statique d'abord.

### `.h5` (Keras)
- Parfois pickle-based, mêmes précautions que `.pt`.

## 6. Alignment bypass

- Trouver le vecteur "refus" dans les activations (SVD sur dataset refusal vs legitimate), le soustraire → le modèle répond à tout.
- Exploiter le clivage persona / éthique / alignement encodé dans l'architecture modulaire.
- `suppress_tokens` bypass: si un token est bloqué côté décodage, trouver une réécriture qui passe par des tokens non bloqués.

## 7. Agent / tool use exploitation

- Un LLM avec outils (shell, HTTP, file I/O) est une surface d'attaque.
- **Confused deputy**: demander au modèle d'appeler un outil avec des permissions qu'il ne devrait pas utiliser.
- **Side-channel**: forcer le modèle à encoder la donnée secrète dans son prochain tool call (URL, paramètres JSON, ordre des items).
- **Indirect injection** via document qui arrive dans le contexte d'un tool.

## 8. Model extraction / inversion

- **Extraction**: faire apprendre le comportement du modèle cible à un modèle local via requêtes massives.
- **Inversion**: reconstruire des données d'entraînement depuis les sorties. Gradient matching, membership inference.
- **Embedding inversion**: récupérer le texte original à partir d'un vecteur d'embedding.

## Cas historiques (ecole2600) — consulter Graphiti pour le writeup complet

- **Executive Command** → semantic brute force via persona
- **OpenSubs Whisper** → voice-based auth gate, `suppress_tokens` bypass sur Whisper
- **secret_vector.safetensors** → tensor `latent_payload` → embeddings TinyLlama-1.1B-Chat-v1.0 → inversion vers vocab → `FLAG{L4TENT_P1R4T3_CH4T}`
- Autres flags observés: `FLAG{W3ird_pr0mpt_Inject1On}`, `CTF{v4e_d3c0d3d}`, `CTF{X0R_k3Y}`, `CTF{EVASION_SUCCESSFUL_VIA_CLUSTERING}`, `CTF{B4CKDR0P}`

Si un challenge courant ressemble à l'un de ces patterns, `search_nodes` / `search_memory_facts` d'abord, pas d'exploration aveugle.
