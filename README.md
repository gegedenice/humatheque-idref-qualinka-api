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

## Organization alignment

`POST /align/thesis-organizations` aligns the organization block of the same
extraction to IdRef PPNs — the fields the person routes ignore:

```json
{
  "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
  "co_tutelle_institutions": [],
  "doctoral_school": "Ecole doctorale 493 ERASME",
  "defense_year": "2015"
}
```

### Why the method is different from persons

Persons are an open universe: millions of authorities, homonyms everywhere, so
candidates must be generated live by Qualinka and disambiguated with
bibliographic evidence. Organizations are the opposite:

- The universe is **closed and small** — 231 ABES establishment codes, a few
  hundred doctoral schools — and every thesis reuses the same entities.
- The risk is not homonymy but **temporal versions of the same institution**:
  "Paris 4" / "Université Paris-Sorbonne" / "Sorbonne Université" are three PPNs
  for one continuous entity, and only the defense year separates them.
- Extracted labels are administratively fuzzy but lexically close to an official
  or variant form ("Université de Paris - Sorbonne" vs "Université Paris-Sorbonne").

So there is **no candidate-generation service and no embedding mode here**. An
embedding would make "Université Paris-Sorbonne" and "Sorbonne Université" look
identical, which is exactly the confusion to avoid. Candidates are resolved
against a **local referential** built once offline, scored with string
similarity, and filtered by validity window.

### The local referential

Built by `idref_org_alignment/`, from two public sources (no Qualinka):

| Step | Source | What it gives |
|---|---|---|
| `parse_abes.py` | `documentation_abes_codes_etab.htm` | the 231 ABES establishment codes |
| `harvest_idref.py` A | `theses.fr/api/v1/theses/recherche/?q=nnt:*CODE*&nombre=1` | `etabSoutenancePpn` — the code → PPN join, which exists nowhere else |
| `harvest_idref.py` B | `https://www.idref.fr/{ppn}.json` | UNIMARC `210$a/$c` (label + validity window), `410$a` (variant forms), `510` (predecessors, successors, members, doctoral schools) |
| `harvest_idref.py` C | `data.idref.fr` SPARQL, one query on `prefLabel ~ "^École doctorale"` | the doctoral schools, which establishment records do not list |
| `build_index.py` | — | `index_recherche.json` + `index_recherche.meta.json` (build date, source endpoints, coverage counts) |

Step B is what makes the whole thing work. ABES calls `PA04` **"Paris 4"**; the
extraction says **"Université de Paris - Sorbonne"**. Those never match at any
threshold. The `410$a` variant list contains *"Université de Paris-Sorbonne"*,
which normalizes to an exact hit. Median 6 labels per entity.

Build it first (needs network access to `theses.fr` and `www.idref.fr`, ~15 min,
fully cached and resumable):

```bash
cd idref_org_alignment && python run_all.py && python lookup.py --selftest
```

Current coverage: **208 / 231** ABES codes resolved to a PPN (the misses are
pre-NNT historical entries, e.g. a 1896 doctorate — no NNT, so no join key),
**268 establishments** and **516 doctoral schools**, 421 of which carry an ED
number and 396 a parent establishment.

`IDREF_ORG_INDEX_PATH` points at the resulting `index_recherche.json`. If the
file is missing the organization routes return `503` with a build instruction;
`/align/person` is unaffected.

### Index structure

Not a hierarchical JSON, not a graph database: a **flat list keyed by PPN with
adjacency lists on each node** — which *is* an adjacency-list graph, the standard
representation at this size. It stays one file, adds no dependency, and a linear
scan of ~780 nodes with `SequenceMatcher` costs ~2 ms. The PPN is both the output
and the join key of every edge, so ABES codes become a plain list (several codes
can point at one PPN).

```json
{
  "type": "etablissement",
  "ppn": "026403633",
  "codes": ["PA04"],
  "label_officiel": "Université Paris-Sorbonne",
  "labels": ["Université Paris-Sorbonne", "Paris 4", "Université de Paris-Sorbonne",
             "Université Paris IV", "Université de Paris 4"],
  "numeros": [],
  "date_debut": "1970",
  "date_fin": "2017",
  "predecesseurs": [],
  "successeurs": [{"ppn": "221333754", "label": "Sorbonne Université"}],
  "ecoles_doctorales": ["149119070", "..."]
}
```

Doctoral-school nodes are the same shape with `"type": "ecole_doctorale"`,
`numeros: ["493"]` and the reciprocal `etablissements: ["02640463X", ...]`.

### Alignment logic

`align_organization` runs, for one label:

1. Build the **candidate pool**. Institutions: all establishments. Doctoral
   schools: if `parent_ppn` is known, only that institution's schools — widened
   to the schools of its predecessors and successors, since a school outlives the
   institution version it was created under. Empty scoped pool falls back to
   global. Reported as `candidate_scope` and `pool_size`.
2. **Score every candidate** against *all* its labels, keeping the best.
3. **Filter by validity window**: candidates valid at the defense year win as a
   block; if none qualifies, all of them are ranked instead.
4. **Decide** `accepted` / `ambiguous` / `low_confidence` / `not_found`.
5. On a year-invalid top match, attach a **`redirect`** to its successor valid
   that year.
6. Optionally, on a local miss, run the **remote SPARQL fallback**.

The composite route does step 1's real work: it aligns `granting_institution`
first and passes its PPN as `parent_ppn` for `doctoral_school`. On the example
above that takes the pool from 516 candidates to 3.

### Score calculation

One string score, no weights to tune, deliberately **not** the person
`name_similarity` — that one has no stopword handling, which matters here
("Université **de** Paris"):

```text
score = 0.5 * SequenceMatcher(normalized_label, normalized_candidate_label)
      + 0.4 * Jaccard(tokens_label, tokens_candidate)      # stopwords removed
      + 0.1 * (1 if tokens_label ⊆ tokens_candidate else 0)
```

Normalization is the existing `normalize_text`: accents stripped, lowercased,
punctuation to spaces. Tokens additionally drop French/English stopwords
(`de`, `du`, `la`, `et`, `the`, `of`, …).

- **Sequence ratio** catches word order and small typos.
- **Jaccard** catches reordered or truncated forms ("Sorbonne Paris Nord,
  Université de").
- **Inclusion bonus** rewards an extracted label that is a strict subset of an
  official one ("Paris 13" ⊂ "Université Paris 13 Nord").

**ED number override.** If the query contains a doctoral-school number
(`"ED 472"`, `"ED472"`, `"Ecole doctorale 493 ERASME"`) and the candidate carries
the same number in `numeros`, the score floors at `0.95`. It is a decisive
identifier, and it survives a complete renaming of the school.

The final score is the maximum over the candidate's label variants — never an
average, so one good variant is enough.

### Thresholds and decision

```text
accept_threshold = 0.72
margin_threshold = 0.08
low_threshold    = 0.45
floor_threshold  = 0.30
```

```text
if top < floor_threshold:                        status = "not_found"
elif top < low_threshold:                        status = "low_confidence"
elif top >= accept_threshold and margin >= margin_threshold:
                                                 status = "accepted"
elif top >= accept_threshold:                    status = "ambiguous"
else:                                            status = "low_confidence"
```

`ppn` is set only when `status` is `accepted`. Thresholds are higher than the
person ones (`0.72` vs `0.65`) because the labels are much shorter and a closed
universe makes near-misses cheap to reject: the correct answer is either in the
referential with a matching variant, or it is not there at all.

The two-floor split exists to separate the two failure modes for the human
validation queue: `low_confidence` means *something plausible was found and needs
a look*, `not_found` means *nothing in the referential resembles this*.

### Temporal redirect

Validity windows come from `210$c`, so they are authoritative rather than guessed.
When the best-scoring entity is out of its window for the defense year and has a
successor valid that year, the response carries:

```json
"redirect": {
  "reason": "matched_label_out_of_period",
  "from_ppn": "026403633",
  "ppn": "221333754",
  "label_officiel": "Sorbonne Université",
  "date_debut": "2018",
  "date_fin": null
}
```

This is **evidence for a human, never an automatic acceptance** — a 2020 thesis
whose cover says "Université Paris-Sorbonne" is a cataloging question, not a
string-matching one. In most cases the redirect is not even needed: the successor
usually carries the old name among its own `410$a` variants and simply wins the
ranking.

### Remote fallback

`allow_remote: true`, **off by default**. Fires only when the local result is
`not_found` or `low_confidence`. It runs a live `data.idref.fr` SPARQL query on
`foaf:Organization` / `skos:prefLabel`, filtered on the longest non-generic token
of the label (searching on "universitat" returns half of Europe before
Heidelberg), then rescores the hits with the same local similarity.

Results are returned **separately** under `remote`, flagged
`source: "idref_sparql"`, and never merged into `candidates`. It covers foreign
co-tutelle partners and master's-only institutions, which are absent from the
ABES table by construction. Expect 30–95 s: the endpoint is slow, so the request
timeout is raised to at least 90 s for this call only.

### `POST /align/thesis-organizations`

The route the pipeline calls.

Request body:

```json
{
  "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
  "co_tutelle_institutions": [],
  "doctoral_school": "Ecole doctorale 493 ERASME",
  "defense_year": "2015",
  "allow_remote": false,
  "max_candidates": 5,
  "accept_threshold": 0.72,
  "margin_threshold": 0.08,
  "low_threshold": 0.45,
  "floor_threshold": 0.30
}
```

The canonical field is `year`, as everywhere else in this API, so the same request
shape generalizes to non-thesis sources. The extraction schema's own key
`defense_year` is accepted as an alias, so its block can also be posted as-is.
Every threshold is per-request overridable, like `/align/person`.

Example:

```bash
curl -X POST "http://localhost:8000/align/thesis-organizations" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
    "co_tutelle_institutions": [],
    "doctoral_school": "Ecole doctorale 493 ERASME",
    "defense_year": "2015"
  }'
```

Response shape — one `/align/organization` result per input field,
`co_tutelle_institutions` being a list of them, `null` for an empty input field:

```jsonc
{
  "source": "idref_org_referential",
  "query": {
    "granting_institution": "Université de Paris 13 Sorbonne Paris Cité",
    "co_tutelle_institutions": [],
    "doctoral_school": "Ecole doctorale 493 ERASME",
    "year": "2015"
  },
  "granting_institution": {
    "source": "idref_org_referential",
    "query": {"label": "...", "kind": "institution", "year": "2015", "parent_ppn": null},
    "candidate_scope": "global",
    "pool_size": 268,
    "status": "accepted",
    "ppn": "02640463X",
    "label_officiel": "Université Sorbonne Paris Nord",
    "score": 0.9815,
    "margin": 0.2448,
    "redirect": null,
    "candidates": [
      {
        "ppn": "02640463X",
        "label_officiel": "Université Sorbonne Paris Nord",
        "type": "etablissement",
        "codes": ["PA13"],
        "score": 0.9815,
        "valid_for_year": true,
        "date_debut": "1970",
        "date_fin": null,
        "url": "https://www.idref.fr/02640463X"
      }
    ],
    "error": null
  },
  "co_tutelle_institutions": [],
  "doctoral_school": {
    "candidate_scope": "parent",   // scoped by the institution PPN above
    "pool_size": 3,                // instead of 516
    "status": "accepted",
    "ppn": "177537957",
    "label_officiel": "École doctorale Érasme",
    "score": 0.7583,
    "margin": 0.2206
  }
}
```

### `POST /align/organization`

One label at a time — the primitive the composite route is built on. Useful to
re-align a single field after a human correction, or to align an organization
that did not come from a thesis block.

| Field | Default | Description |
|---|---|---|
| `label` | required | Extracted organization label |
| `kind` | `"institution"` | `"institution"` or `"doctoral_school"` |
| `year` | empty | Defense year; selects the temporal version (`defense_year` accepted as an alias) |
| `parent_ppn` | empty | Institution PPN scoping a doctoral-school search |
| `allow_remote` | `false` | SPARQL fallback on a local miss |
| `max_candidates` | `5` | Candidates returned |
| `accept_threshold` | `0.72` | Minimum score to accept |
| `margin_threshold` | `0.08` | Minimum top-vs-second margin |
| `low_threshold` | `0.45` | Below this, `low_confidence` |
| `floor_threshold` | `0.30` | Below this, `not_found` |
| `timeout`, `retries`, `backoff` | env defaults | Remote fallback only |

```bash
curl -X POST "http://localhost:8000/align/organization" \
  -H "Content-Type: application/json" \
  -d '{"label": "Ecole doctorale 493 ERASME", "kind": "doctoral_school",
       "year": "2015", "parent_ppn": "02640463X"}'
```

### `GET /org-index/search`

Inspection of the referential, matching the `/find-person`, `/attrra/{ppn}`,
`/references/{ppn}` convention. Same scoring, same response shape.

| Parameter | Required | Description |
|---|---:|---|
| `q` | yes | Label to score against the referential |
| `year` | no | Defense year |
| `type` | no | `etablissement` (default) or `ecole_doctorale` |
| `parent_ppn` | no | Institution PPN scoping doctoral schools |
| `max_results` | no | Default `5` |

```bash
curl "http://localhost:8000/org-index/search?q=Paris%2013&year=2015"
```

### Known limits

- **Pre-1985 institutions.** 23 ABES codes have no PPN because they predate the
  NNT, so no thesis carries them. Those labels fall back to a global lexical
  match or `not_found`.
- **Non-IdRef structures.** `"doctoral_school": "U.E.N. HISTOIRES"` — a pre-ED
  Sorbonne structure — returns `not_found` (best score `0.28`). There is no
  authority record to find; abstaining is the correct answer, not a threshold to
  tune away.
- **120 doctoral schools have no parent link**, so `parent_ppn` scoping cannot
  reach them; they are still found globally.
- The referential is a **snapshot**. Re-run `run_all.py` after institutional
  mergers; caches make the re-run cheap.

## Authentication

Authentication is optional.

Set `IDREF_API_KEY` in `.env` to require clients to send:

```text
X-API-Key: <your key>
```

If `IDREF_API_KEY` is empty, endpoints are public.

## Environment variables

Copy `.example.env` to `.env` and adjust values.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | HTTP server port |
| `IDREF_API_KEY` | empty | Optional API key |
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
| `IDREF_ORG_INDEX_PATH` | `idref_org_alignment/index_recherche.json` | Local organization referential; missing file makes the organization routes return `503` |
| `IDREF_ORG_SPARQL_ENDPOINT` | `https://data.idref.fr/sparql` | Endpoint of the opt-in remote organization fallback |
| `IDREF_ORG_ACCEPT_THRESHOLD` | `0.72` | Minimum organization score to accept |
| `IDREF_ORG_MARGIN_THRESHOLD` | `0.08` | Minimum top-vs-second organization margin |
| `IDREF_ORG_LOW_THRESHOLD` | `0.45` | Below this, organization status is `low_confidence` |
| `IDREF_ORG_FLOOR_THRESHOLD` | `0.30` | Below this, organization status is `not_found` |
| `IDREF_ORG_MAX_CANDIDATES` | `5` | Default organization candidate limit |

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
- Organization alignment makes **no** network call in its default mode: it reads
  the local referential only, so it is fast enough for batch use. Only
  `allow_remote: true` reaches `data.idref.fr`, and that endpoint answers in
  30-95 s.
- The organization referential is loaded lazily into a module global on first use
  and never reloaded; restart the service after rebuilding `index_recherche.json`.
  `index_recherche.meta.json`, written next to it by `build_index.py`, says when
  the index was built and from which endpoints — check it before suspecting the
  aligner of being stale.
