# Humatheque IdRef Qualinka API

FastAPI service for aligning extracted person names to French IdRef authority
records. The service is designed for a cataloging pipeline where metadata has
first been extracted from thesis, dissertation, or memoir cover images, and the
next step is to find the most plausible IdRef PPN for each extracted person.

The API is intentionally deterministic: it generates candidate authority PPNs,
fetches evidence for each candidate, computes transparent evidence scores, and
returns either an accepted PPN or an abstention status.

## Why this service

The input data usually contains extracted fields such as:

- author
- advisor
- jury president
- reviewers
- committee members
- title
- discipline
- institution
- doctoral school
- degree type
- defense year

The service aligns one person at a time. For example, given the extracted name
`Valérie Robert` and the surrounding document metadata, it tries to identify the
corresponding IdRef authority PPN by using authority-record evidence
and bibliographic-neighborhood evidence.

## External APIs used

### Qualinka `find-ra-idref`

Endpoint:

```text
https://qualinka.idref.fr/data/find-ra-idref/api/v2/req
```

This service is used for candidate generation. It accepts a parsed last name and
optional first name:

```text
?lastName=robert&firstName=val%C3%A9rie
```

It returns candidate IdRef person authority PPNs. It is preferred over a simple
hand-written IdRef Solr query because it compacts multiple IdRef-specific search
strategies and handles person-name lookup better.

### Qualinka `attrra`

Endpoint:

```text
https://qualinka.idref.fr/data/attrra/api/v2/req?ra_id=<PPN>
```

This service returns for a authority PPN the informations of the authority record. The most
useful fields are:

- `preferedform`: preferred authority label, used for name matching.
- `source`: bibliographic source text attached to the authority record.
- `noteGen`: general notes, often containing degree, institution, discipline, or year.
- `bioNote`: biographical notes, used as note evidence when present.

For thesis alignment, `attrra.source`, `attrra.noteGen`, and `attrra.bioNote`
can be stronger than generic linked references because they often describe
exactly why the authority record was created.

### IdRef `references`

Endpoint:

```text
https://www.idref.fr/services/references/<PPN>.json
```

This service returns bibliographic records linked to an authority, grouped by
role. Here, roles are kept as explainability metadata but are not
used as a strong ranking signal. 
(For example a thesis advisor may mostly appear as an author
in IdRef, and role labels can introduce bias.)

## Alignment logic

`POST /align/person` runs the full flow.

1. Parse the submitted `name` into first name and last name.
2. Optionally use `first_name` and `last_name` overrides when automatic parsing is uncertain.
3. Query Qualinka `find-ra-idref` to get candidate PPNs.
4. For each candidate PPN:
   - fetch `attrra`
   - fetch IdRef `references`
   - extract preferred labels, authority notes (`noteGen` and `bioNote`), authority sources, and reference citations
5. Build the current document context from:
   - person name
   - title
   - subtitle
   - discipline
   - institution
   - doctoral school
   - degree type
   - year
6. Score each candidate with separated evidence components.
7. Rank candidates by final score.
8. Accept only if the top candidate passes the threshold and has enough margin over the second candidate.

The service uses two different kinds of similarity:

- string similarity for the authority name itself -> lexical score
- semantic similarity for bibliographic evidence such as `attrra.source`, `attrra.noteGen`, `attrra.bioNote`, and IdRef reference citations -> semantic score

The name score is always string-based. The bibliographic semantic scores can run
in either lexical mode or embedding mode.

### Similarity modes

#### String similarity for names

The `name` score compares the extracted person name with candidate authority
forms using normalized string similarity (custom fuzzy score using Python SequenceMatcher).

Normalization:

- remove accents
- lowercase
- replace punctuation with spaces
- compare token overlap and character similarity

This is deliberately not embedding-based. Names need strict identity evidence;
an embedding model could make two different people look close because their
names or topics are semantically nearby.

#### Lexical semantic similarity

This is the default mode for bibliographic evidence with a simple bag-of-words count vector (similar in spirit to CountVectorizer).

It is used when:

```json
"embedding_model": ""
```

and when `.env` contains:

```env
IDREF_EMBEDDING_MODEL=
```

The service builds normalized token-count vectors and computes cosine
similarity. This is lightweight, deterministic, and does not load any ML model.

#### Embedding semantic similarity

Embedding mode is used when a sentence-transformers encoder model name is supplied.

Per request:

```json
{
  "name": "Valérie Robert",
  "title": "...",
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

Globally through `.env`:

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

In embedding mode, the service encodes the current document context and each
candidate evidence text with `SentenceTransformer(...).encode(...,
normalize_embeddings=True)`, then computes dot-product similarity. Because
vectors are normalized, the dot product is cosine similarity.

Embedding mode may improve bibliographic proximity, especially when the wording
differs between the extracted metadata and IdRef evidence. 
(But first modle loading also increases first-request latency).

The `/align/person` response includes:

```json
"similarity": {
  "type": "lexical",
  "model": null
}
```

or:

```json
"similarity": {
  "type": "embedding",
  "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}
```

## Score calculation

Each candidate receives five component scores.

### `name`

String similarity between the extracted person name and the candidate authority
forms:

- `attrra.preferedform[*].value`
- candidate first name + last name from `find-ra-idref`

This is kept separate from semantic similarity so a candidate with a close topic
but a poor name match does not win too easily.

### `attrra_source`

Best semantic similarity between the current document context and each
`attrra.source` value.

This uses lexical semantic similarity by default, or embedding semantic
similarity when `embedding_model` / `IDREF_EMBEDDING_MODEL` is set.

This is heavily weighted because `source` can contain thesis-like evidence such
as title, date, institution, and author name.

### `attrra_note`

Best semantic similarity between the current document context and each
`attrra.noteGen` or `attrra.bioNote` value.

This uses the same semantic mode as `attrra_source`.

This is useful when notes contain information such as:

```text
Titulaire d'un doctorat d'université en médecine spécialisée (Nancy 1,2003)
Auteur d'une thèse en Sciences cognitives, psychologie et neurocognition à Université Grenoble Alpes en 2023
```

### `references`

Top-k average semantic similarity between the current document context and the
candidate's linked reference citations from IdRef.

The service uses top-k rather than averaging all references. This avoids
penalizing prolific authors whose broad bibliography would dilute the signal.

Default:

```text
reference_top_k = 3
```

### `institution_year`

Small deterministic consistency score:

- `+0.50` if the extracted institution appears in candidate evidence
- `+0.25` if the extracted doctoral school appears in candidate evidence
- `+0.25` if the extracted year appears in candidate evidence

The score is capped at `1.0`.

### Final Score

```text
final =
  weight_name * name
+ weight_attrra_source * attrra_source
+ weight_attrra_note * attrra_note
+ weight_references * references
+ weight_institution_year * institution_year
```

Default weights:

```text
weight_name = 0.40
weight_attrra_source = 0.25
weight_attrra_note = 0.15
weight_references = 0.15
weight_institution_year = 0.05
```

Default thresholds:

```text
accept_threshold = 0.65
margin_threshold = 0.08
```

Decision logic:

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

`best_ppn` is set only when `status` is `accepted`.

## API endpoints

Interactive documentation is available at:

```text
/docs
```

### `GET /health`

Container health check.

Response:

```json
{"ok": true}
```

### `GET /find-person`

Runs only candidate generation through Qualinka `find-ra-idref`.

Query parameters:

| Parameter | Required | Description |
|---|---:|---|
| `name` | no | Full person name to parse |
| `first_name` | no | Override parsed first name |
| `last_name` | no | Override parsed last name |
| `max_results` | no | Maximum candidates, default `20` |

Either `name` or `last_name` must be supplied.

Example:

```bash
curl "http://localhost:8000/find-person?name=Val%C3%A9rie%20Robert"
```

### `GET /attrra/{ppn}`

Fetches Qualinka `attrra` evidence for one IdRef PPN.

Example:

```bash
curl "http://localhost:8000/attrra/076642860"
```

### `GET /references/{ppn}`

Fetches linked IdRef bibliographic references for one PPN.

Example:

```bash
curl "http://localhost:8000/references/076642860?max_docs_per_role=10"
```

### `POST /align/person`

Runs the full alignment pipeline.

Request body:

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
  "margin_threshold": 0.08,
  "weight_name": 0.40,
  "weight_attrra_source": 0.25,
  "weight_attrra_note": 0.15,
  "weight_references": 0.15,
  "weight_institution_year": 0.05
}
```

`embedding_model` controls semantic scoring for bibliographic evidence:

- empty string: lexical token cosine similarity
- non-empty sentence-transformers model name: embedding cosine similarity

Example:

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

Response shape:

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

## Authentication

Authentication is optional.

Set `API_KEY` in `.env` to require clients to send:

```text
X-API-Key: <your key>
```

If `API_KEY` is empty, endpoints are public.

## Environment variables

Copy `.example.env` to `.env` and adjust values.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | HTTP server port |
| `API_KEY` | empty | Optional API key |
| `FIND_RA_ENDPOINT` | Qualinka public endpoint | Candidate generation endpoint |
| `ATTRRA_ENDPOINT` | Qualinka public endpoint | Authority enrichment endpoint |
| `REFERENCES_ENDPOINT` | IdRef public endpoint | Linked references endpoint |
| `IDREF_USER_AGENT` | `humatheque-idref-qualinka-api/0.1` | User-Agent sent to public services |
| `IDREF_HTTP_TIMEOUT` | `20.0` | HTTP timeout in seconds |
| `IDREF_MAX_RETRIES` | `2` | Retry count for external requests |
| `IDREF_BACKOFF_BASE` | `1.0` | Exponential backoff base |
| `IDREF_MAX_CANDIDATES` | `20` | Default candidate limit |
| `IDREF_MAX_DOCS_PER_ROLE` | `20` | Default reference docs per role |
| `IDREF_REFERENCE_TOP_K` | `3` | Number of best reference matches averaged |
| `IDREF_ACCEPT_THRESHOLD` | `0.65` | Minimum score to accept |
| `IDREF_MARGIN_THRESHOLD` | `0.08` | Minimum top-vs-second score margin |
| `IDREF_EMBEDDING_MODEL` | empty | Optional default sentence-transformers model for embedding semantic similarity |
| `IDREF_WEIGHT_NAME` | `0.40` | Default weight for name similarity |
| `IDREF_WEIGHT_ATTRRA_SOURCE` | `0.25` | Default weight for Qualinka `attrra.source` similarity |
| `IDREF_WEIGHT_ATTRRA_NOTE` | `0.15` | Default weight for Qualinka `noteGen`/`bioNote` similarity |
| `IDREF_WEIGHT_REFERENCES` | `0.15` | Default weight for IdRef reference citation similarity |
| `IDREF_WEIGHT_INSTITUTION_YEAR` | `0.05` | Default weight for institution, doctoral school, and year consistency |

Embedding mode for all requests:

```env
IDREF_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

When this variable is empty, all requests use lexical semantic similarity unless
the request body explicitly provides `embedding_model`.

## Local run

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

The Docker image follows the same deployment style as
`humatheque-postgres-api`: Python slim image, `requirements.txt`, non-root user,
and `uvicorn app:app`.

## Operational notes

- The service performs blocking HTTP calls to public IdRef and Qualinka
  endpoints. FastAPI runs them in a threadpool for API endpoints.
- Handled upstream failures are returned inside candidate `errors` or response
  `error` fields where possible.
- The alignment is conservative: it can abstain rather than force an IdRef PPN.
- Role labels from IdRef references are preserved in evidence but not used as a
  strong scoring feature.
- For batch jobs, keep `max_candidates` and `max_docs_per_role` bounded to avoid
  slow calls against public services.
