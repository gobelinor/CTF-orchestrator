---
name: ctf-cloud-solver
description: Solveur CTF Cloud. Charge la methodology core et applique les quick wins et outils spécifiques AWS/GCP/Azure/Kubernetes, IAM, secrets, metadata services, containers.
---

# CTF Cloud Solver

Applique la Resolution Loop définie dans `ctf-core-methodology`. Ce skill ajoute uniquement les signaux et outils spécifiques cloud.

## Quick Wins
- `aws sts get-caller-identity`, `gcloud auth list`, `az account show`, `kubectl config current-context`
- Buckets/blobs publics, env vars, secrets dans images ou pipelines
- IAM trop large, service account réutilisable, kube token, dashboard exposé
- Metadata service accessible depuis un container ou une app du challenge
- Registry / artefact CI contenant creds ou manifests

## High-Value Pivots
- **Identity-first.** Savoir qui tu es avant de lister quoi que ce soit.
- Extraire artefacts locaux d'abord: `.env`, kubeconfig, Terraform, Compose, CI files, service accounts.
- Énumérer uniquement les ressources liées aux indices explicites de l'énoncé.
- Exploiter la trust relationship utile (bucket public, rôle mal lié, RBAC permissif).
- Au step Research: Graphiti + web pour la techno exacte (ECS/EKS/Fargate, GKE Workload Identity, Azure Managed Identity, etc.).

## Règles spécifiques cloud
- **Jamais** de scan d'un compte / tenant / cluster entier sans indice explicite.
- Pas de perturbation de workloads, pas de stress test.
- Rester dans les ressources du challenge.

## Tools spécifiques
`aws`, `gcloud`, `az`, `kubectl`, `docker`, `yq`, scripts Python `boto3` / `google-cloud` / `azure-sdk` si CLI insuffisant.
