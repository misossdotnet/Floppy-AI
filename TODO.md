# TODO - Floppy-AI

## P0 - Securite / exposition

- [x] Supprimer les valeurs par defaut sensibles (`FLASK_SECRET_KEY`, `POSTGRES_PASSWORD`) hors environnement local.
- [x] Desactiver `debug=True` hors environnement local.
- [x] Ne plus retourner `str(exc)` brut en API/UI: erreurs publiques normalisees + logs serveur.
- [x] Ajouter authN/authZ sur routes destructives et metier (`delete`, `imports`, `build-dataset`, `approve`, `mcp`).
- [x] Proteger les endpoints API de lecture sensible (`GET /chunks`, `/documents/<id>/lineage`, `/dataset-builds/<id>`) avec auth + scopes.
- [x] Interdire l'usage de token via query/form (`api_token`) et accepter uniquement les headers (`Authorization`, `X-Floppy-Token`, `X-Api-Token`).
- [x] Systeme de connexion UI par session admin.
- [ ] Roles UI fins au-dela du role admin unique.
- [ ] JWT signe pour API avec expiration + rotation/revocation.
- [ ] Rate limiting sur endpoints API/MCP et endpoints publics WebChat/QuizBot.
- [ ] Politique CORS explicite: origines, methodes, headers autorises.
- [ ] Audit transversal des actions sensibles metier (`delete`, `approve`, `build`) hors audit LLM/QuizBot.

#### P1 - Integrite donnees / fiabilite

- [x] Corriger les colonnes `last_date_edit` vers `timestamptz DEFAULT now()`.
- [x] Ajouter des contraintes FK logiques entre `project`, `shard`, `chunk`, `train`.
- [x] Ajouter des index metier sur filtres frequents (`project_slug`, `quality_score`, `approval_status`, `shard_id`).
- [x] Introduire une strategie de migrations SQL versionnees avec runner + checksums.
- [ ] Extraire le schema runtime `ensure_*` vers de vrais fichiers SQL dans `app/migrations/`.
- [ ] Ajouter un controle de migration dans le demarrage Docker/CI.
- [x] Eviter le scan de tous les projets en priorisant `document_registry` pour retrouver un document.
- [x] Eviter la creation implicite de tables sur certains chemins de lecture metier.
- [x] Centraliser la validation des payloads REST + MCP.
- [x] Ajouter des docstrings sur les fonctions/services critiques.
- [ ] Supprimer les imports wildcard (`from services import *`) au profit d'imports explicites.


#### P2 - Performance / maintenabilite
- [x] Eviter le scan de tous les projets pour retrouver un document (`find_document_record`)
- [x] Eviter la creation implicite de tables en chemins de lecture quand non necessaire
- [x] Centraliser la validation des payloads (schemas) pour REST + MCP
- [x] Structurer `app.py` en modules (`db`, `services`, `api_rest`, `api_mcp`, `ui`)
- [ ] Supprimer les imports wildcard (`from services import *`) au profit d'imports explicites
- [x] Ajouter des docstrings sur les fonctions/services critiques (auditabilite + maintenabilite)

## 1) Securite et acces
- [ ] Systeme de connexion UI (session + roles)
- [ ] Systeme de connexion API (token/JWT + scopes)
- [ ] Systeme de connexion MCP (token service + ACL outils)
- [ ] Rate limiting sur endpoints API/MCP
- [ ] Journal d'audit des actions sensibles (delete/approve/build)

## 2) Validation humaine
- [ ] Ecran de revue document:
  - [ ] inspecter source
  - [ ] voir texte normalise
  - [ ] voir chunks
  - [ ] voir metadonnees
  - [ ] valider / rejeter
  - [ ] annoter anomalies
  - [ ] exclure sections

## 3) Normalisation documentaire
- [ ] Pipeline de normalisation versionne
- [ ] Stockage multi-formes:
  - [ ] `raw_content`
  - [ ] `normalized_content`
  - [ ] `rendered_text`
  - [ ] `structured_content`
- [ ] Regles avancees:
  - [ ] nettoyage markdown/html
  - [ ] tableaux
  - [ ] blocs de code
  - [ ] listes
  - [ ] hierarchie de titres
  - [ ] extraction liens/images/notes
  - [ ] detection langue
  - [ ] detection type de contenu

## 4) Chunking avance
- [ ] Strategies:
  - [ ] token window
  - [ ] section markdown
  - [ ] hybride section + token
  - [ ] code-aware
  - [ ] table-aware
  - [ ] fusion petits paragraphes
  - [ ] contraintes zones strictes
- [ ] Metadata RAG enrichies:
  - [ ] `section_path`
  - [ ] `heading`
  - [ ] `summary_short`
  - [ ] `previous_chunk_id`
  - [ ] `next_chunk_id`
  - [ ] `document_position_ratio`

## 5) Qualite donnees
- [ ] Moteur de regles qualite:
  - [ ] document vide
  - [ ] chunk trop court / trop long
  - [ ] bruit caracteres anormal
  - [ ] langue incoherente
  - [ ] repetitions fortes
  - [ ] markdown casse
  - [ ] PII sensible
- [ ] Score qualite global (document/chunk/dataset)
- [ ] Explication du score (why/trace)

## 6) Deduplication
- [ ] Hash exact brut (`sha256_raw`)
- [ ] Hash normalise (`sha256_normalized`)
- [ ] Near-duplicate (`simhash` ou equivalent)
- [ ] Politique configurable:
  - [ ] rejet
  - [ ] conservation taggee
  - [ ] dedup au build

## 7) Dataset builds et exports
- [ ] Faire de `dataset_build` un artefact complet:
  - [ ] snapshot source
  - [ ] version normalisation/chunking
  - [ ] regles de filtrage
  - [ ] stats
  - [ ] checksum
- [ ] Exports:
  - [ ] JSONL
  - [ ] Parquet
  - [ ] Markdown nettoye
  - [ ] CSV
  - [ ] embeddings-ready JSON
  - [ ] manifest YAML/JSON

## 8) Observabilite et QA
- [ ] Tests unitaires sur services metier
- [ ] Tests d'integration API REST
- [ ] Tests de contrat MCP (`initialize`, `tools/list`, `tools/call`)
- [ ] CI: lint + tests + migration check
- [ ] KPIs metier:
  - [ ] nb documents/chunks/builds
  - [ ] score qualite moyen
  - [ ] taux approbation/rejet
  - [ ] taux duplication
  - [ ] estimation tokens
  - [ ] build N vs build N+1

## 9) Definition of Done (DoD)
- [ ] Aucune route metier sensible sans auth
- [x] Aucun secret de dev par defaut en prod
- [x] Migrations DB versionnees et rejouables
- [ ] Couverture de tests minimale: 70% sur services metier critiques
- [ ] Docs README synchronisees avec les endpoints reels
