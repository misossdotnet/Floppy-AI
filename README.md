# Floppy-AI

- Datas Warehouse
- LLM Data Engineering Platform - Dataset Factory
- /!\ Not ready for production.

## Contexte

`Floppy-AI` est une application de type `Datas Warehouse` dediee a la gestion de donnees pour le pre-training des LLM. Elle prepare aussi une base solide pour le RAG, la memoire persistante, la construction d'agents assistants specialises et le test de leurs comportements conversationnels.

Le besoin principal est de:

- centraliser des documents par projet;
- stocker les versions "source" (shards);
- produire des jeux de donnees de qualite exploitables pour le pre-training LLM;
- preparer des corpus structurables pour des workflows RAG;
- maintenir des traces reutilisables comme memoire persistante;
- construire et tester des assistants agents specialises autour de ces donnees.
- comparaison de modeles Ollama locaux, benchmarks bilingues, metriques et exports

L'objectif est d'avoir un socle simple, traceable et reproductible pour passer d'un document brut (Markdown/HTML exporte) a des segments enrichis de metadata de navigation et a des jeux de donnees evaluables.

## Application du logiciel

Le logiciel sert de couche de preparation des donnees entre:

- la source de contenu (CMS, Notion, markdown exporte, etc.);
- les pipelines de pre-training / fine-tuning LLM;
- les workflows d'orchestration (n8n, scripts Python, jobs batch).

En pratique, le `Datas Warehouse` permet:

- de creer des projets et leurs tables dediees;
- de stocker les documents "longs" dans des shards;
- de decouper ces shards en chunks avec overlap;
- de conserver la tracabilite et la qualite des donnees pour l'entrainement des modeles.
