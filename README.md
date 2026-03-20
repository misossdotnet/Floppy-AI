# Floppy-AI

> LLM Data Engineering Platform
> Datas Warehouse - Dataset Factory

## Contexte

`Floppy-AI` est une application interne de type `Datas Warehouse` dediee a la gestion de donnees pour le pre-training des LLM.

Le besoin principal est de:

- centraliser des documents par projet;
- stocker les versions "source" (shards);
- produire des jeux de donnees de qualite exploitables pour le pre-training LLM (et les workflows RAG en second niveau).

L'objectif est d'avoir un socle simple, traceable et reproductible pour passer d'un document brut (Markdown) a des segments enrichis de metadata de navigation.

## Application du logiciel

Le logiciel sert de couche de preparation des donnees entre:

- la source de contenu (CMS, Notion, markdown exporte, etc.);
- les pipelines de pre-training / fine-tuning LLM;
- les workflows d'orchestration (n8n, scripts Python, jobs batch).

En pratique, `Floppy-AI` permet:

- de creer des projets et leurs tables dediees;
- de stocker les documents "longs" dans des shards;
- de decouper ces shards en chunks avec overlap;
- de conserver la tracabilite et la qualite des donnees pour l'entrainement des modeles.

## Fonctionnalites principales

1. Creation de projet
   - Saisie du nom dans l'interface.
   - Transformation automatique en slug.
   - Provisioning des tables `{slug}_shard`, `{slug}_chunk`, `{slug}_train`, `{slug}_chat` et `{slug}_chat_evaluation`.
   - Insertion dans `public.project`.
2. Vue projet / shards / chunks / train
   - Affichage des shards par projet.
   - Comptage des chunks relies via `chunk.shard_id = shard.uuid`.
   - Visualisation detaillee des shards et des chunks (metadata + contenu complet).
   - Saisie des exemples de conversation dans `{slug}_train`.
3. Vue chat + evaluation
   - Liste des conversations par `session_id` dans `{slug}_chat`.
   - Visualisation des messages groupes par `session_id`.
   - Formulaire d'evaluation d'une session (5 ratings + commentaire) dans `{slug}_chat_evaluation` (upsert sur `session_id`).
4. Dashboard chat_evaluation
   - KPI qualite (sessions evaluees, score global, moyennes par axe, taux excellentes/problematiques).
   - Evolution hebdomadaire des scores.
   - Relation longueur de session vs score global.
5. Preparation dataset pour pre-training LLM
   - Decoupage prioritaire par sections Markdown (`#`, `##`, `###`).
   - Re-decoupage si depassement de la taille cible.
   - Parametres de tokenisation ajustables.
   - Metadata de contexte (`previous_document_id`, `previous_chunk_id`, `next_chunk_id`, etc.).

## Architecture technique

- Backend: `Flask` (Python)
- Base de donnees: `PostgreSQL`
- Conteneurisation: `Docker / Docker Compose`
- Code applicatif: dossier `app/`

## Modele de donnees

Le schema de reference est documente dans [app_skull.sql](docs/app_skull.sql).