# API Humatheque IdRef Qualinka

Service FastAPI pour aligner des noms de personnes extraits avec des notices
d'autorite françaises IdRef. Le service est conçu pour un pipeline de
catalogage dans lequel des metadonnees ont d'abord été extraites d'images de
pages de titre de theses ou memoires, et ou l'étape suivante
consiste à trouver le PPN IdRef le plus plausible pour chaque personne extraite.

L'API est volontairement deterministe : elle génère des PPN candidats
d'autorites, récupère des indices pour chaque candidat, calcule des scores
transparents, puis retourne soit un PPN accepté, soit un statut d'abstention.

## Pourquoi Ce Service Existe

Les données d'entrée contiennent generalement des champs extraits comme :

- auteur
- directeur
- president du jury
- rapporteurs
- membres du jury
- titre
- discipline
- etablissement
- ecole doctorale
- type de diplome
- annee de soutenance

Le service aligne une personne à la fois. Par exemple, à partir du nom extrait
`Valérie Robert` et des metadonnées documentaires environnantes, il essaie
d'identifier le PPN de l'autorite IdRef correspondante en utilisant pour chaque autorité candidate 
les indices issus de la notice d'autorite et les indices du voisinage bibliographique.

## API Externes Utilisees

### Qualinka `find-ra-idref`

Endpoint :

```text
https://qualinka.idref.fr/data/find-ra-idref/api/v2/debug/req
```

Ce service est utilise pour générer les candidats. Il accepte un nom de famille
analyse et un prénom optionnel :

```text
?lastName=robert&firstName=val%C3%A9rie
```

Il retourne des PPN candidats d'autorites personnes IdRef. Il est préféré à une
simple requete Solr IdRef écrite à la main parce qu'il compacte plusieurs
stratégies de recherche propres à IdRef et gère mieux la recherche par nom de
personne.

### Qualinka `attrra`

Endpoint :

```text
https://qualinka.idref.fr/data/attrra/api/v2/req?ra_id=<PPN>
```

Ce service retourne pour PPN d'autorité donné les informations issues de la notice
d'autorité. Les champs les plus utiles sont :

- `preferedform` : libellé préféré de l'autorité, utilisé pour comparer les noms.
- `source` : texte de source bibliographique rattaché à la notice d'autorité.
- `noteGen` : notes générales, contenant souvent le diplôme, l'établissement, la discipline ou l'année.

Pour l'alignement de theses, `attrra.source` et `attrra.noteGen` peuvent être
plus forts que les références liées génériques, car ils décrivent souvent
precisément pourquoi la notice d'autorite a été créée.

### IdRef `references`

Endpoint :

```text
https://www.idref.fr/services/references/<PPN>.json
```

Ce service retourne les notices bibliographiques liées à une autorite, groupées
par rôle. Ici, les roles sont conservés comme metadonnées
d'explicabilité, mais ils ne sont pas utilisés comme signal fort de classement.
(Par exemple, un directeur de thèse peut apparaitre principalement comme auteur dans IdRef, et
les libellés de rôle peuvent introduire un biais.)

## Logique D'alignement

`POST /align/person` exécute le flux complet.

1. Analyser le `name` soumis en prénom et nom.
2. Utiliser optionnellement les surcharges `first_name` et `last_name` lorsque l'analyse automatique est incertaine.
3. Interroger Qualinka `find-ra-idref` pour obtenir les PPN candidats.
4. Pour chaque PPN candidat :
   - récupérer `attrra`
   - récupérer les `references` IdRef
   - extraire les formes préférées, notes d'autorité, sources d'autorité et citations de references
5. Construire le contexte du document courant à partir de :
   - nom de la personne
   - titre
   - sous-titre
   - discipline
   - établissement
   - ecole doctorale
   - type de diplôme
   - année
6. Noter chaque candidat avec des composantes d'indices separées.
7. Classer les candidats par score final.
8. Accepter uniquement si le meilleur candidat dépasse le seuil et dispose d'une marge suffisante sur le deuxième candidat.

Le service utilise deux types de similarité différents :

- similarité lexicale de chaine pour le nom d'autorité lui-même -> score `name` lexical
- similarité sémantique pour les indices bibliographiques comme `attrra.source`, `attrra.noteGen` et les citations de références IdRef -> score sémantique

Le score de nom est toujours fondé sur des chaines. Les scores sémantiques
bibliographiques peuvent fonctionner soit en mode lexical, soit en mode
embedding.

### Modes de similarité

#### Similarité de chaine pour les noms

Le score `name` compare le nom de personne extrait avec les formes d'autorité
candidates au moyen d'une similarité de chaine normalisée (fuzzy score basé sur la classe python SequenceMatcher).

Normalisation :

- suppression des accents
- passage en minuscules
- remplacement de la ponctuation par des espaces
- comparaison du recouvrement des tokens et de la similarité de caractères

Ce score n'est volontairement pas fondé sur des embeddings. Les noms exigent un
indice d'identité strict ; un modèle d'embedding pourrait rendre deux personnes
differentes proches parce que leurs noms ou leurs sujets sont semantiquement
voisins.

#### Similarite sémantique lexicale

C'est le mode par défaut pour les indices bibliographiques avec un simple bag-of-words + count vector (similaire dans l'esprit à CountVectorizer)

Il est utilisé lorsque :

```json
"embedding_model": ""
```

et lorsque `.env` contient :

```env
IDREF_EMBEDDING_MODEL=
```

Le service construit des vecteurs de tokens normalisés basés sur du comptage d'occurrences et calcule
une similarité cosinus. C'est léger, déterministe, et aucun modèle de machine
learning n'est chargé.

#### Similarite sémantique par embedding

Le mode embedding est utilisé lorsqu'un nom de modèle encoder sentence-transformers est
fourni.

Par requete :

```json
{
  "name": "Valérie Robert",
  "title": "...",
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

Globalement via `.env` :

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

En mode embedding, le service encode le contexte du document courant et chaque
texte d'indice candidat avec `SentenceTransformer(...).encode(...,
normalize_embeddings=True)`, puis calcule une similarité par produit scalaire.
Comme les vecteurs sont normalisés, le produit scalaire correspond à une
similarite cosinus.

Le mode embedding peut améliorer la proximité bibliographique, surtout lorsque
la formulation differe entre les metadonnées extraites et les indices IdRef. 
(Mais le chargement du modèle augmente aussi la latence de la premiere requete).

La réponse de `/align/person` inclut :

```json
"similarity": {
  "type": "lexical",
  "model": null
}
```

ou :

```json
"similarity": {
  "type": "embedding",
  "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

## Calcul Du Score

Chaque candidat reçoit cinq scores de composantes.

### `name`

Similarité de chaine entre le nom de personne extrait et les formes d'autorité
candidates :

- `attrra.preferedform[*].value`
- prénom + nom du candidat provenant de `find-ra-idref`

Ce score est séparé de la similarité semantique afin qu'un candidat ayant un
sujet proche mais un mauvais appariement de nom ne "gagne" pas trop facilement.

### `attrra_source`

Meilleure similarité sémantique entre le contexte du document courant et chaque
valeur de `attrra.source`.

Ce score utilise par defaut la similarité sémantique lexicale, ou la similarité
sémantique par embedding lorsque `embedding_model` / `IDREF_EMBEDDING_MODEL` est
defini.

Il est fortement pondéré parce que `source` peut contenir des indices proches
d'une thèse, comme le titre, la date, l'établissement et le nom de l'auteur.

### `attrra_note`

Meilleure similarité sémantique entre le contexte du document courant et chaque
valeur de `attrra.noteGen`.

Ce score utilise le même mode sémantique que `attrra_source`.

Il est utile lorsque les notes contiennent des informations comme :

```text
Titulaire d'un doctorat d'université en médecine spécialisée (Nancy 1,2003)
```

### `references`

Moyenne top-k des similarités sémantiques entre le contexte du document courant
et les citations de références liées au candidat dans IdRef.

Le service utilise un top-k plutot qu'une moyenne de toutes les références. Cela
évite de pénaliser les auteurs prolifiques dont la bibliographie large diluerait
le signal.

Par defaut :

```text
reference_top_k = 3
```

### `institution_year`

Petit score déterministe de cohérence :

- `+0.50` si l'établissement extrait apparait dans les indices du candidat
- `+0.25` si l'école doctorale extraite apparait dans les indices du candidat
- `+0.25` si l'année extraite apparait dans les indices du candidat

Le score est plafonne à `1.0`.

### Score Final

```text
final =
  0.40 * name
+ 0.25 * attrra_source
+ 0.15 * attrra_note
+ 0.15 * references
+ 0.05 * institution_year
```

Seuils par défaut :

```text
accept_threshold = 0.65
margin_threshold = 0.08
```

Logique de décision :

```text
if no candidates:
    status = "not_found"
elif top.final < accept_threshold:
    status = "low_confidence"
elif top.final - second.final < margin_threshold:
    status = "ambiguous"
else:
    status = "accepted"
```

`best_ppn` n'est renseigné que lorsque `status` vaut `accepted`.

## Endpoints API

La documentation interactive est disponible à :

```text
/docs
```

### `GET /health`

Controle de santé du conteneur.

Réponse :

```json
{"ok": true}
```

### `GET /find-person`

Exécute uniquement la génération de candidats via Qualinka `find-ra-idref`.

Paramètres de requête :

| Paramètre | Requis | Description |
|---|---:|---|
| `name` | non | Nom complet de la personne à analyser |
| `first_name` | non | Surcharge du prénom analyse |
| `last_name` | non | Surcharge du nom analyse |
| `max_results` | non | Nombre maximum de candidats, par defaut `20` |

`name` ou `last_name` doit etre fourni.

Exemple :

```bash
curl "http://localhost:8000/find-person?name=Val%C3%A9rie%20Robert"
```

### `GET /attrra/{ppn}`

Recupère les indices Qualinka `attrra` pour un PPN IdRef.

Exemple :

```bash
curl "http://localhost:8000/attrra/076642860"
```

### `GET /references/{ppn}`

Recupère les références bibliographiques IdRef liées à un PPN.

Exemple :

```bash
curl "http://localhost:8000/references/076642860?max_docs_per_role=10"
```

### `POST /align/person`

Exécute le pipeline complet d'alignement.

Corps de requête :

```json
{
  "name": "Valérie Robert",
  "title": "Satisfaction et vécu périopératoire des patients opérés sous anesthésie péribulbaire",
  "subtitle": "",
  "discipline": "médecine spécialisée",
  "institution": "Nancy 1",
  "doctoral_school": "",
  "degree_type": "Thèse d'exercice",
  "year": "2003",
  "max_candidates": 20,
  "max_docs_per_role": 20,
  "reference_top_k": 3,
  "embedding_model": "",
  "accept_threshold": 0.65,
  "margin_threshold": 0.08
}
```

`embedding_model` controle le scoring sémantique des indices bibliographiques :

- chaine vide : similarité cosinus lexicale sur tokens
- nom de modèle sentence-transformers non vide : similarité cosinus par embedding

Exemple :

```bash
curl -X POST "http://localhost:8000/align/person" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "name": "Valérie Robert",
    "title": "Satisfaction et vécu périopératoire des patients opérés sous anesthésie péribulbaire",
    "discipline": "médecine spécialisée",
    "institution": "Nancy 1",
    "degree_type": "Thèse d'exercice",
    "year": "2003"
  }'
```

Forme de la réponse :

```jsonc
{
  "source": "idref_qualinka_alignment",
  "similarity": {
    "type": "lexical",
    "model": null
  },
  "query": {
    "name": "Valérie Robert",
    "title": "...",
    "subtitle": "",
    "discipline": "médecine spécialisée",
    "institution": "Nancy 1",
    "doctoral_school": "",
    "degree_type": "Thèse d'exercice",
    "year": "2003"
  },
  "candidate_search": {
    "full_name": "Valérie Robert",
    "first_name": "Valérie",
    "last_name": "Robert",
    "parsed_first_name": "Valérie",
    "parsed_last_name": "Robert",
    "url": "https://qualinka.idref.fr/...",
    "error": null
  },
  "status": "accepted",
  "best_ppn": "076642860",
  "best_candidate": {
    "ppn": "076642860",
    "first_name": "Valérie",
    "last_name": "Robert",
    "url": "https://www.idref.fr/076642860",
    "score": {
      "final": 0.6783,
      "name": 1.0,
      "attrra_source": 0.743,
      "attrra_note": 0.421,
      "references": 0.0,
      "institution_year": 0.75
    },
    "evidence": {
      "preferred_forms": ["Robert, Valérie"],
      "best_attrra_source": "...",
      "best_attrra_note": "...",
      "best_references": []
    },
    "errors": []
  },
  "candidates": []
}
```

## Authentification

L'authentification est optionnelle.

Définir `API_KEY` dans `.env` pour obliger les clients à envoyer :

```text
X-API-Key: <your key>
```

Si `API_KEY` est vide, les endpoints sont publics.

## Variables D'environnement

Copier `.example.env` vers `.env` et ajuster les valeurs.

| Variable | Defaut | Description |
|---|---|---|
| `PORT` | `8000` | Port du serveur HTTP |
| `API_KEY` | vide | Clé API optionnelle |
| `FIND_RA_ENDPOINT` | endpoint public Qualinka | Endpoint de génération de candidats |
| `ATTRRA_ENDPOINT` | endpoint public Qualinka | Endpoint d'enrichissement d'autorité |
| `REFERENCES_ENDPOINT` | endpoint public IdRef | Endpoint de références liées |
| `IDREF_USER_AGENT` | `humatheque-idref-qualinka-api/0.1` | User-Agent envoyé aux services publics |
| `IDREF_HTTP_TIMEOUT` | `20.0` | Timeout HTTP en secondes |
| `IDREF_MAX_RETRIES` | `2` | Nombre de tentatives pour les requêtes externes |
| `IDREF_BACKOFF_BASE` | `1.0` | Base de backoff exponentiel |
| `IDREF_MAX_CANDIDATES` | `20` | Limite de candidats par defaut |
| `IDREF_MAX_DOCS_PER_ROLE` | `20` | Nombre de documents de référence par rôle par défaut |
| `IDREF_REFERENCE_TOP_K` | `3` | Nombre de meilleures références moyennees |
| `IDREF_ACCEPT_THRESHOLD` | `0.65` | Score minimum pour accepter |
| `IDREF_MARGIN_THRESHOLD` | `0.08` | Marge minimale entre le premier et le deuxieme score |
| `IDREF_EMBEDDING_MODEL` | vide | Modèle sentence-transformers optionnel par defaut pour la similarité sémantique par embedding |

Mode embedding pour toutes les requêtes :

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Lorsque cette variable est vide, toutes les requêtes utilisent la similarité
sémantique lexicale, sauf si le corps de requête fournit explicitement
`embedding_model`.

## Execution Locale

```bash
cp .example.env .env
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## Docker

```bash
docker build -t humatheque-idref-qualinka-api .
docker run --env-file .env -p 8000:8000 humatheque-idref-qualinka-api
```

## Notes opérationnelles

- Le service effectue des appels HTTP bloquants vers les endpoints publics
  IdRef et Qualinka. FastAPI les execute dans un threadpool pour les endpoints
  API.
- Les echecs en amont sont retournés dans les champs `errors` des candidats
  ou dans les champs `error` des réponses lorsque c'est possible.
- L'alignement est conservateur : préfère s'abstenir plutot que forcer un PPN
  IdRef.
- Les libellés de rôle des références IdRef sont conservés dans les indices,
  mais ne sont pas utilisés comme signal fort de scoring.
- Pour les traitements par lots, garder `max_candidates` et
  `max_docs_per_role` bornes afin d'éviter des appels lents aux services
  publics.
