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

## Architecture technique

- Backend: `Flask` (Python)
- Base de donnees: `PostgreSQL`
- Conteneurisation: `Docker / Docker Compose`
- Code applicatif: dossier `app/`

## Modele de donnees

Le schema de reference est documente dans [app_skull.sql](docs/app_skull.sql).
