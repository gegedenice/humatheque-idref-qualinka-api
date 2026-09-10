"""FastAPI service for IdRef person alignment with Qualinka/Paprika evidence."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader
from pydantic import AliasChoices, BaseModel, Field

load_dotenv()


FIND_RA_ENDPOINT = os.getenv(
    "FIND_RA_ENDPOINT",
    "https://qualinka.idref.fr/data/find-ra-idref/api/v2/debug/req",
)
ATTRRA_ENDPOINT = os.getenv(
    "ATTRRA_ENDPOINT",
    "https://qualinka.idref.fr/data/attrra/api/v2/req",
)
REFERENCES_ENDPOINT = os.getenv(
    "REFERENCES_ENDPOINT",
    "https://www.idref.fr/services/references/{ppn}.json",
)
USER_AGENT = os.getenv("IDREF_USER_AGENT", "humatheque-idref-qualinka-api/0.1")
API_KEY = os.getenv("IDREF_API_KEY", "")
HF_TOKEN = os.getenv("HF_TOKEN", "") or None
RETRIED_STATUS = {429, 500, 502, 503, 504}

DEFAULT_TIMEOUT = float(os.getenv("IDREF_HTTP_TIMEOUT", "20.0"))
DEFAULT_RETRIES = int(os.getenv("IDREF_MAX_RETRIES", "2"))
DEFAULT_BACKOFF = float(os.getenv("IDREF_BACKOFF_BASE", "1.0"))
DEFAULT_MAX_CANDIDATES = int(os.getenv("IDREF_MAX_CANDIDATES", "20"))
DEFAULT_MAX_DOCS_PER_ROLE = int(os.getenv("IDREF_MAX_DOCS_PER_ROLE", "20"))
DEFAULT_REFERENCE_TOP_K = int(os.getenv("IDREF_REFERENCE_TOP_K", "3"))
DEFAULT_ACCEPT_THRESHOLD = float(os.getenv("IDREF_ACCEPT_THRESHOLD", "0.65"))
DEFAULT_MARGIN_THRESHOLD = float(os.getenv("IDREF_MARGIN_THRESHOLD", "0.08"))
DEFAULT_EMBEDDING_MODEL = os.getenv("IDREF_EMBEDDING_MODEL", "")
DEFAULT_WEIGHT_NAME = float(os.getenv("IDREF_WEIGHT_NAME", "0.40"))
DEFAULT_WEIGHT_ATTRRA_SOURCE = float(os.getenv("IDREF_WEIGHT_ATTRRA_SOURCE", "0.25"))
DEFAULT_WEIGHT_ATTRRA_NOTE = float(os.getenv("IDREF_WEIGHT_ATTRRA_NOTE", "0.15"))
DEFAULT_WEIGHT_REFERENCES = float(os.getenv("IDREF_WEIGHT_REFERENCES", "0.15"))
DEFAULT_WEIGHT_INSTITUTION_YEAR = float(os.getenv("IDREF_WEIGHT_INSTITUTION_YEAR", "0.05"))

ORG_INDEX_PATH = os.getenv(
    "IDREF_ORG_INDEX_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "idref_org_alignment", "index_recherche.json"),
)
ORG_SPARQL_ENDPOINT = os.getenv("IDREF_ORG_SPARQL_ENDPOINT", "https://data.idref.fr/sparql")
DEFAULT_ORG_ACCEPT_THRESHOLD = float(os.getenv("IDREF_ORG_ACCEPT_THRESHOLD", "0.72"))
DEFAULT_ORG_MARGIN_THRESHOLD = float(os.getenv("IDREF_ORG_MARGIN_THRESHOLD", "0.08"))
DEFAULT_ORG_LOW_THRESHOLD = float(os.getenv("IDREF_ORG_LOW_THRESHOLD", "0.45"))
DEFAULT_ORG_FLOOR_THRESHOLD = float(os.getenv("IDREF_ORG_FLOOR_THRESHOLD", "0.30"))
DEFAULT_ORG_MAX_CANDIDATES = int(os.getenv("IDREF_ORG_MAX_CANDIDATES", "5"))

EMBEDDER = None
EMBEDDING_CACHE: dict[str, list[float]] = {}
ORG_INDEX: dict[str, Any] | None = None

app = FastAPI(
    title="Humatheque IdRef Qualinka API",
    version="0.1.0",
    description="Resolve extracted person names to IdRef PPN candidates with Qualinka and IdRef evidence.",
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    if API_KEY and api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class HttpTuning(BaseModel):
    """Transport knobs shared by every request model."""

    timeout: float = Field(DEFAULT_TIMEOUT, gt=0.0, le=120.0)
    retries: int = Field(DEFAULT_RETRIES, ge=0, le=10)
    backoff: float = Field(DEFAULT_BACKOFF, ge=0.0, le=30.0)


class AlignPersonRequest(HttpTuning):
    name: str = Field(..., description="Extracted full person name.")
    first_name: str = Field("", description="Optional parsed first-name override.")
    last_name: str = Field("", description="Optional parsed last-name override.")
    title: str = Field("", description="Extracted document title.")
    subtitle: str = Field("", description="Extracted document subtitle.")
    discipline: str = Field("", description="Extracted discipline.")
    institution: str = Field("", description="Extracted granting institution.")
    doctoral_school: str = Field("", description="Extracted doctoral school.")
    degree_type: str = Field("", description="Extracted degree or document type.")
    year: str = Field("", description="Extracted defense year.")
    max_candidates: int = Field(DEFAULT_MAX_CANDIDATES, ge=1, le=100)
    max_docs_per_role: int = Field(DEFAULT_MAX_DOCS_PER_ROLE, ge=0, le=200)
    reference_top_k: int = Field(DEFAULT_REFERENCE_TOP_K, ge=1, le=20)
    embedding_model: str = Field(
        DEFAULT_EMBEDDING_MODEL,
        description=(
            "Optional sentence-transformers model. Leave empty for lexical token cosine similarity. "
            "Set a model name, for example sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2, "
            "to use embedding-based semantic similarity."
        ),
    )
    accept_threshold: float = Field(DEFAULT_ACCEPT_THRESHOLD, ge=0.0, le=1.0)
    margin_threshold: float = Field(DEFAULT_MARGIN_THRESHOLD, ge=0.0, le=1.0)
    weight_name: float = Field(DEFAULT_WEIGHT_NAME, ge=0.0)
    weight_attrra_source: float = Field(DEFAULT_WEIGHT_ATTRRA_SOURCE, ge=0.0)
    weight_attrra_note: float = Field(DEFAULT_WEIGHT_ATTRRA_NOTE, ge=0.0)
    weight_references: float = Field(DEFAULT_WEIGHT_REFERENCES, ge=0.0)
    weight_institution_year: float = Field(DEFAULT_WEIGHT_INSTITUTION_YEAR, ge=0.0)


class FindPersonResponse(BaseModel):
    source: str
    query: dict[str, Any]
    found: int
    returned: int
    results: list[dict[str, Any]]
    error: str | None = None


@dataclass
class EvidenceScore:
    name: float = 0.0
    attrra_source: float = 0.0
    attrra_note: float = 0.0
    references: float = 0.0
    institution_year: float = 0.0
    final: float = 0.0


@dataclass
class Candidate:
    ppn: str
    first_name: str | None = None
    last_name: str | None = None
    attrra: dict[str, Any] | None = None
    references: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    score: EvidenceScore = field(default_factory=EvidenceScore)
    evidence: dict[str, Any] = field(default_factory=dict)


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_similarity(left: Any, right: Any) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    intersection = left_tokens & right_tokens
    token_f1 = (
        2 * len(intersection) / (len(left_tokens) + len(right_tokens))
        if left_tokens and right_tokens
        else 0.0
    )
    char_ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    return 0.6 * token_f1 + 0.4 * char_ratio


def text_vector(value: str) -> dict[str, float]:
    vector: dict[str, float] = {}
    for token in normalize_text(value).split():
        if len(token) <= 2:
            continue
        vector[token] = vector.get(token, 0.0) + 1.0
    return vector


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[token] * right[token] for token in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def lexical_similarity(left: str, right: str) -> float:
    return cosine_similarity(text_vector(left), text_vector(right))


def load_embedder(model_name: str) -> Any:
    global EMBEDDER
    if EMBEDDER is None:
        from sentence_transformers import SentenceTransformer

        EMBEDDER = SentenceTransformer(model_name, token=HF_TOKEN)
    return EMBEDDER


def embedding_vector(text: str, model_name: str) -> list[float]:
    cache_key = f"{model_name}\0{text}"
    if cache_key not in EMBEDDING_CACHE:
        model = load_embedder(model_name)
        EMBEDDING_CACHE[cache_key] = model.encode(text, normalize_embeddings=True).tolist()
    return EMBEDDING_CACHE[cache_key]


def embedding_similarity(left: str, right: str, model_name: str) -> float:
    left_vec = embedding_vector(left, model_name)
    right_vec = embedding_vector(right, model_name)
    return sum(a * b for a, b in zip(left_vec, right_vec))


def semantic_similarity(left: str, right: str, embedding_model: str | None = None) -> float:
    if not left.strip() or not right.strip():
        return 0.0
    if embedding_model:
        return max(0.0, embedding_similarity(left, right, embedding_model))
    return lexical_similarity(left, right)


def similarity_mode(embedding_model: str | None) -> dict[str, str | None]:
    model = embedding_model or None
    return {
        "type": "embedding" if model else "lexical",
        "model": model,
    }


def request_json(url: str, timeout: float, retries: int, backoff: float,
                 accept: str = "application/json") -> tuple[Any | None, str | None]:
    last_error = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload), None
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code not in RETRIED_STATUS:
                break
        except URLError as exc:
            last_error = f"URL error: {exc.reason}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(backoff * (2**attempt))
    return None, last_error


def parse_person_name(full_name: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", full_name.strip())
    if "," in cleaned:
        last, first = [part.strip() for part in cleaned.split(",", 1)]
        return first, last

    particles = {"de", "du", "des", "del", "della", "van", "von", "le", "la"}
    parts = cleaned.split()
    if len(parts) <= 1:
        return "", cleaned

    last_start = len(parts) - 1
    while last_start > 0 and normalize_text(parts[last_start - 1]) in particles:
        last_start -= 1
    return " ".join(parts[:last_start]), " ".join(parts[last_start:])


def common_authority_record(ppn: str, first_name: Any = None, last_name: Any = None) -> dict[str, Any]:
    title = " ".join(str(part) for part in (first_name, last_name) if part)
    return {
        "source": "idref",
        "id": ppn,
        "ppn": ppn,
        "title": title or None,
        "authors": None,
        "abstract": None,
        "doi": None,
        "pdf_url": None,
        "url": f"https://www.idref.fr/{ppn}",
        "year": None,
        "date": None,
        "doc_type": "authority-person",
        "journal": None,
        "first_name": first_name,
        "last_name": last_name,
    }


def iter_candidate_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [item for item in payload.get("ids", []) if isinstance(item, dict)]

    items = []
    if isinstance(payload, list):
        for block in payload:
            if isinstance(block, dict):
                items.extend(item for item in block.get("results", []) if isinstance(item, dict))
    return items


def find_candidates(
    full_name: str,
    first_name_override: str | None,
    last_name_override: str | None,
    timeout: float,
    retries: int,
    backoff: float,
    max_candidates: int,
) -> tuple[list[Candidate], dict[str, Any]]:
    parsed_first_name, parsed_last_name = parse_person_name(full_name)
    first_name = first_name_override or parsed_first_name
    last_name = last_name_override or parsed_last_name
    meta = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "parsed_first_name": parsed_first_name,
        "parsed_last_name": parsed_last_name,
        "url": None,
        "error": None,
    }
    if not last_name:
        meta["error"] = "Missing last name."
        return [], meta

    query = {"lastName": last_name}
    if first_name:
        query["firstName"] = first_name
    url = f"{FIND_RA_ENDPOINT}?{urlencode(query)}"
    payload, error = request_json(url, timeout, retries, backoff)
    meta["url"] = url
    meta["error"] = error
    if error:
        return [], meta

    candidates: list[Candidate] = []
    seen = set()
    for item in iter_candidate_items(payload):
        ppn = str(item.get("ppn") or "").strip()
        if not ppn or ppn in seen:
            continue
        seen.add(ppn)
        candidates.append(
            Candidate(
                ppn=ppn,
                first_name=item.get("firstName"),
                last_name=item.get("lastName"),
            )
        )
        if len(candidates) >= max_candidates:
            return candidates, meta
    return candidates, meta


def fetch_attrra(candidate: Candidate, timeout: float, retries: int, backoff: float) -> None:
    url = f"{ATTRRA_ENDPOINT}?{urlencode({'ra_id': candidate.ppn})}"
    payload, error = request_json(url, timeout, retries, backoff)
    if error:
        candidate.errors.append(f"attrra: {error}")
        return
    candidate.attrra = payload if isinstance(payload, dict) else None


def fetch_references(
    candidate: Candidate,
    timeout: float,
    retries: int,
    backoff: float,
    max_docs_per_role: int,
) -> None:
    url = REFERENCES_ENDPOINT.format(ppn=candidate.ppn)
    payload, error = request_json(url, timeout, retries, backoff)
    if error:
        candidate.errors.append(f"references: {error}")
        return
    if not isinstance(payload, dict):
        candidate.references = None
        return

    for role in iter_reference_roles(payload):
        for docs_key in ("docs", "doc"):
            docs = role.get(docs_key)
            if isinstance(docs, list):
                role[docs_key] = docs[:max_docs_per_role]
            elif isinstance(docs, dict) and max_docs_per_role == 0:
                role[docs_key] = []
    candidate.references = payload


def as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def attrra_notes(attrra: dict[str, Any]) -> list[str]:
    return as_text_list(attrra.get("noteGen")) + as_text_list(attrra.get("bioNote"))


def preferred_forms(candidate: Candidate) -> list[str]:
    forms = []
    attrra = candidate.attrra or {}
    for item in attrra.get("preferedform", []):
        if isinstance(item, dict) and item.get("value"):
            forms.append(str(item["value"]))
    joined_name = " ".join(part for part in [candidate.first_name, candidate.last_name] if part)
    if joined_name:
        forms.append(joined_name)
    return forms


def listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_reference_roles(refs: dict[str, Any]) -> list[dict[str, Any]]:
    roles = [role for role in listify(refs.get("roles")) if isinstance(role, dict)]

    for service_payload in refs.values():
        if not isinstance(service_payload, dict):
            continue
        result = service_payload.get("result")
        if not isinstance(result, dict):
            continue
        roles.extend(role for role in listify(result.get("role")) if isinstance(role, dict))

    return roles


def reference_citations(candidate: Candidate) -> list[dict[str, str]]:
    refs = candidate.references or {}
    citations = []
    for role in iter_reference_roles(refs):
        role_name = str(role.get("role_name") or role.get("roleName") or "")
        for docs_key in ("docs", "doc"):
            for doc in listify(role.get(docs_key)):
                if isinstance(doc, dict) and doc.get("citation"):
                    citations.append({"role": role_name, "citation": str(doc["citation"])})
    return citations


def current_context(payload: AlignPersonRequest) -> str:
    parts = [
        payload.name,
        payload.title,
        payload.subtitle,
        payload.discipline,
        payload.institution,
        payload.doctoral_school,
        payload.degree_type,
        payload.year,
    ]
    return " ".join(part for part in parts if part)


def ranked_similarities(
    query: str,
    texts: list[str],
    embedding_model: str | None,
) -> list[tuple[float, str]]:
    ranked = [(semantic_similarity(query, text, embedding_model), text) for text in texts]
    return sorted(ranked, key=lambda item: item[0], reverse=True)


def best_similarity(
    query: str,
    texts: list[str],
    embedding_model: str | None,
) -> tuple[float, str | None]:
    ranked = ranked_similarities(query, texts, embedding_model)
    if not ranked:
        return 0.0, None
    return ranked[0]


def top_k_average_similarity(
    query: str,
    texts: list[str],
    embedding_model: str | None,
    top_k: int,
) -> tuple[float, list[str]]:
    ranked = ranked_similarities(query, texts, embedding_model)[:top_k]
    if not ranked:
        return 0.0, []
    return sum(score for score, _ in ranked) / len(ranked), [text for _, text in ranked]


def institution_year_score(payload: AlignPersonRequest, candidate: Candidate) -> float:
    attrra = candidate.attrra or {}
    evidence_text = " ".join(
        attrra_notes(attrra)
        + as_text_list(attrra.get("source"))
        + [item["citation"] for item in reference_citations(candidate)]
    )
    score = 0.0
    if payload.institution and normalize_text(payload.institution) in normalize_text(evidence_text):
        score += 0.5
    if payload.doctoral_school and normalize_text(payload.doctoral_school) in normalize_text(evidence_text):
        score += 0.25
    if payload.year and re.search(rf"\b{re.escape(str(payload.year))}\b", evidence_text):
        score += 0.25
    return min(score, 1.0)


def score_candidate(payload: AlignPersonRequest, candidate: Candidate) -> None:
    forms = preferred_forms(candidate)
    candidate.score.name = max((name_similarity(payload.name, form) for form in forms), default=0.0)

    query = current_context(payload)
    attrra = candidate.attrra or {}
    sources = as_text_list(attrra.get("source"))
    notes = attrra_notes(attrra)
    refs = reference_citations(candidate)
    ref_texts = [item["citation"] for item in refs]

    embedding_model = payload.embedding_model or None
    candidate.score.attrra_source, best_source = best_similarity(query, sources, embedding_model)
    candidate.score.attrra_note, best_note = best_similarity(query, notes, embedding_model)
    candidate.score.references, best_refs = top_k_average_similarity(
        query,
        ref_texts,
        embedding_model,
        payload.reference_top_k,
    )
    candidate.score.institution_year = institution_year_score(payload, candidate)

    candidate.score.final = (
        payload.weight_name * candidate.score.name
        + payload.weight_attrra_source * candidate.score.attrra_source
        + payload.weight_attrra_note * candidate.score.attrra_note
        + payload.weight_references * candidate.score.references
        + payload.weight_institution_year * candidate.score.institution_year
    )
    candidate.evidence = {
        "preferred_forms": forms,
        "best_attrra_source": best_source,
        "best_attrra_note": best_note,
        "best_references": best_refs,
    }


def status_for_ranked(ranked: list[Candidate], accept_threshold: float, margin_threshold: float) -> str:
    if not ranked:
        return "not_found"
    top = ranked[0]
    if top.score.final < accept_threshold:
        return "low_confidence"
    if len(ranked) > 1 and top.score.final - ranked[1].score.final < margin_threshold:
        return "ambiguous"
    return "accepted"


def score_weights_to_json(payload: AlignPersonRequest) -> dict[str, float]:
    return {
        "name": payload.weight_name,
        "attrra_source": payload.weight_attrra_source,
        "attrra_note": payload.weight_attrra_note,
        "references": payload.weight_references,
        "institution_year": payload.weight_institution_year,
    }


def candidate_to_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "ppn": candidate.ppn,
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "url": f"https://www.idref.fr/{candidate.ppn}",
        "score": {
            "final": round(candidate.score.final, 4),
            "name": round(candidate.score.name, 4),
            "attrra_source": round(candidate.score.attrra_source, 4),
            "attrra_note": round(candidate.score.attrra_note, 4),
            "references": round(candidate.score.references, 4),
            "institution_year": round(candidate.score.institution_year, 4),
        },
        "evidence": candidate.evidence,
        "errors": candidate.errors,
    }


def align_person(payload: AlignPersonRequest) -> dict[str, Any]:
    candidates, search_meta = find_candidates(
        payload.name,
        first_name_override=payload.first_name or None,
        last_name_override=payload.last_name or None,
        timeout=payload.timeout,
        retries=payload.retries,
        backoff=payload.backoff,
        max_candidates=payload.max_candidates,
    )

    for candidate in candidates:
        fetch_attrra(candidate, payload.timeout, payload.retries, payload.backoff)
        fetch_references(candidate, payload.timeout, payload.retries, payload.backoff, payload.max_docs_per_role)
        score_candidate(payload, candidate)

    ranked = sorted(candidates, key=lambda item: item.score.final, reverse=True)
    status = status_for_ranked(ranked, payload.accept_threshold, payload.margin_threshold)
    return {
        "source": "idref_qualinka_alignment",
        "similarity": similarity_mode(payload.embedding_model),
        "query": {
            "name": payload.name,
            "title": payload.title,
            "subtitle": payload.subtitle,
            "discipline": payload.discipline,
            "institution": payload.institution,
            "doctoral_school": payload.doctoral_school,
            "degree_type": payload.degree_type,
            "year": payload.year,
        },
        "candidate_search": search_meta,
        "score_weights": score_weights_to_json(payload),
        "status": status,
        "best_ppn": ranked[0].ppn if status == "accepted" else None,
        "best_candidate": candidate_to_json(ranked[0]) if ranked else None,
        "candidates": [candidate_to_json(candidate) for candidate in ranked],
    }


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "humatheque-idref-qualinka-api",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/find-person", response_model=FindPersonResponse)
async def find_person_endpoint(
    name: str = Query("", description="Full person name."),
    first_name: str = Query("", description="Optional first-name override."),
    last_name: str = Query("", description="Optional last-name override."),
    max_results: int = Query(DEFAULT_MAX_CANDIDATES, ge=1, le=100),
    timeout: float = Query(DEFAULT_TIMEOUT, gt=0.0, le=120.0),
    retries: int = Query(DEFAULT_RETRIES, ge=0, le=10),
    backoff: float = Query(DEFAULT_BACKOFF, ge=0.0, le=30.0),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    if not name and not last_name:
        raise HTTPException(status_code=400, detail="Provide name or last_name.")
    candidates, meta = await run_in_threadpool(
        find_candidates,
        name,
        first_name or None,
        last_name or None,
        timeout,
        retries,
        backoff,
        max_results,
    )
    return {
        "source": "qualinka_find_ra_idref",
        "query": {
            "name": name,
            "first_name": meta.get("first_name"),
            "last_name": meta.get("last_name"),
        },
        "found": len(candidates),
        "returned": len(candidates),
        "results": [common_authority_record(c.ppn, c.first_name, c.last_name) for c in candidates],
        "error": meta.get("error"),
    }


@app.get("/attrra/{ppn}")
async def attrra_endpoint(
    ppn: str,
    timeout: float = Query(DEFAULT_TIMEOUT, gt=0.0, le=120.0),
    retries: int = Query(DEFAULT_RETRIES, ge=0, le=10),
    backoff: float = Query(DEFAULT_BACKOFF, ge=0.0, le=30.0),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    candidate = Candidate(ppn=ppn)
    await run_in_threadpool(fetch_attrra, candidate, timeout, retries, backoff)
    return {
        "source": "qualinka_attrra",
        "ppn": ppn,
        "url": f"https://www.idref.fr/{ppn}",
        "record": candidate.attrra,
        "error": "; ".join(candidate.errors) if candidate.errors else None,
    }


@app.get("/references/{ppn}")
async def references_endpoint(
    ppn: str,
    max_docs_per_role: int = Query(DEFAULT_MAX_DOCS_PER_ROLE, ge=0, le=200),
    timeout: float = Query(DEFAULT_TIMEOUT, gt=0.0, le=120.0),
    retries: int = Query(DEFAULT_RETRIES, ge=0, le=10),
    backoff: float = Query(DEFAULT_BACKOFF, ge=0.0, le=30.0),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    candidate = Candidate(ppn=ppn)
    await run_in_threadpool(fetch_references, candidate, timeout, retries, backoff, max_docs_per_role)
    return {
        "source": "idref_references",
        "ppn": ppn,
        "url": f"https://www.idref.fr/services/references/{ppn}.json",
        "references": candidate.references,
        "error": "; ".join(candidate.errors) if candidate.errors else None,
    }


@app.post("/align/person")
async def align_person_endpoint(
    payload: AlignPersonRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await run_in_threadpool(align_person, payload)


# --------------------------------------------------------------------------- #
# Organization alignment (établissements de soutenance + écoles doctorales)
#
# A different problem from persons: the universe is closed and small (~231 ABES
# codes, a few hundred doctoral schools), so we resolve against a locally built
# referential instead of querying candidates live. The risk is not homonymy but
# confusing temporal versions of one institution ("Paris 4" / "Université
# Paris-Sorbonne" / "Sorbonne Université") — the defense year is what settles it,
# which is why this stays strictly lexical and never uses embeddings.
# --------------------------------------------------------------------------- #

ORG_STOPWORDS = {"de", "des", "du", "d", "la", "le", "les", "l", "et", "en",
                 "the", "of", "for", "a"}
ED_NUMBER_RE = re.compile(r"\bed\s*(\d{1,4})\b")
# mots génériques d'organisation : mauvais filtres SPARQL (« universitat » ramène
# toute l'Europe avant Heidelberg). Utilisés seulement pour choisir le terme de
# recherche distant, jamais pour le scoring local.
ORG_GENERIC_TERMS = {"universite", "universitat", "university", "universidad", "universita",
                     "ecole", "school", "institut", "institute", "college", "faculte",
                     "centre", "center", "laboratoire", "hochschule", "universitaet"}


class AlignOrganizationRequest(HttpTuning):
    label: str = Field(..., description="Extracted organization label.")
    kind: str = Field(
        "institution",
        description='Either "institution" (granting / co-tutelle) or "doctoral_school".',
    )
    year: str = Field("", validation_alias=AliasChoices("year", "defense_year"),
                      description="Extracted defense year; disambiguates temporal versions. "
                                  "The extraction schema's defense_year is accepted as an alias.")
    parent_ppn: str = Field(
        "",
        description="Optional institution PPN. For a doctoral school, restricts candidates to that institution's schools.",
    )
    allow_remote: bool = Field(
        False,
        description="On a local miss, fall back to a live data.idref.fr SPARQL search (covers foreign co-tutelle partners).",
    )
    max_candidates: int = Field(DEFAULT_ORG_MAX_CANDIDATES, ge=1, le=50)
    accept_threshold: float = Field(DEFAULT_ORG_ACCEPT_THRESHOLD, ge=0.0, le=1.0)
    margin_threshold: float = Field(DEFAULT_ORG_MARGIN_THRESHOLD, ge=0.0, le=1.0)
    low_threshold: float = Field(DEFAULT_ORG_LOW_THRESHOLD, ge=0.0, le=1.0)
    floor_threshold: float = Field(DEFAULT_ORG_FLOOR_THRESHOLD, ge=0.0, le=1.0)


class AlignThesisOrganizationsRequest(HttpTuning):
    granting_institution: str = Field("", description="Extracted granting institution.")
    co_tutelle_institutions: list[str] = Field(
        default_factory=list, description="Extracted co-tutelle institution names."
    )
    doctoral_school: str = Field("", description="Extracted doctoral school.")
    year: str = Field("", validation_alias=AliasChoices("year", "defense_year"),
                      description="Extracted defense year; defense_year accepted as an alias.")
    allow_remote: bool = Field(False, description="See AlignOrganizationRequest.allow_remote.")
    max_candidates: int = Field(DEFAULT_ORG_MAX_CANDIDATES, ge=1, le=50)
    accept_threshold: float = Field(DEFAULT_ORG_ACCEPT_THRESHOLD, ge=0.0, le=1.0)
    margin_threshold: float = Field(DEFAULT_ORG_MARGIN_THRESHOLD, ge=0.0, le=1.0)
    low_threshold: float = Field(DEFAULT_ORG_LOW_THRESHOLD, ge=0.0, le=1.0)
    floor_threshold: float = Field(DEFAULT_ORG_FLOOR_THRESHOLD, ge=0.0, le=1.0)


def org_tokens(value: str) -> set[str]:
    return {token for token in normalize_text(value).split() if token not in ORG_STOPWORDS}


def org_similarity(left: str, right: str) -> float:
    """Sequence ratio (word order) + token Jaccard (shared words) + inclusion bonus.

    Deliberately not name_similarity: that one is tuned for person names and has no
    stopword handling, which matters here ("Université *de* Paris").
    """
    left_norm, right_norm = normalize_text(left), normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens, right_tokens = org_tokens(left), org_tokens(right)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    inclusion = 1.0 if left_tokens and left_tokens.issubset(right_tokens) else 0.0
    return 0.5 * sequence + 0.4 * jaccard + 0.1 * inclusion


def ed_numbers(value: str) -> set[str]:
    """Doctoral-school numbers ("ED 472", "ED472"). A decisive signal when present."""
    return {match.lstrip("0") or "0" for match in ED_NUMBER_RE.findall(normalize_text(value))}


def load_org_index() -> dict[str, Any]:
    """Load the referential once into {entities, by_ppn}. Same lazy pattern as EMBEDDER."""
    global ORG_INDEX
    if ORG_INDEX is None:
        try:
            with open(ORG_INDEX_PATH, encoding="utf-8") as handle:
                entities = json.load(handle)
        except FileNotFoundError:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Organization referential not built: {ORG_INDEX_PATH} is missing. "
                    "Run idref_org_alignment/run_all.py."
                ),
            )
        ORG_INDEX = {
            "entities": entities,
            "by_ppn": {item["ppn"]: item for item in entities if item.get("ppn")},
        }
    return ORG_INDEX


def org_year(value: Any) -> int | None:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else None


def year_in_window(year: int | None, start: Any, end: Any) -> bool:
    if year is None:
        return True
    low, high = org_year(start), org_year(end)
    if low is not None and year < low:
        return False
    if high is not None and year > high:
        return False
    return True


def org_candidate_pool(index: dict[str, Any], kind: str, parent_ppn: str) -> tuple[list[dict[str, Any]], str]:
    """Doctoral schools are scoped to the parent institution when we know it:
    ~14 candidates instead of ~500. Widened to the parent's predecessors and
    successors, since a school outlives the institution version it was created under."""
    entity_type = "ecole_doctorale" if kind == "doctoral_school" else "etablissement"
    typed = [item for item in index["entities"] if item.get("type") == entity_type]
    if entity_type != "ecole_doctorale" or not parent_ppn:
        return typed, "global"

    parent = index["by_ppn"].get(parent_ppn)
    if not parent:
        return typed, "global"
    related = {parent_ppn}
    for key in ("predecesseurs", "successeurs"):
        related.update(link["ppn"] for link in parent.get(key, []) if link.get("ppn"))
    scoped_ppns = set()
    for ppn in related:
        node = index["by_ppn"].get(ppn)
        if node:
            scoped_ppns.update(node.get("ecoles_doctorales", []))
    scoped = [item for item in typed if item["ppn"] in scoped_ppns]
    return (scoped, "parent") if scoped else (typed, "global")


def score_org_entity(entity: dict[str, Any], label: str, query_numbers: set[str]) -> float:
    labels = entity.get("labels") or [entity.get("label_officiel") or ""]
    score = max((org_similarity(label, form) for form in labels), default=0.0)
    if query_numbers and query_numbers & set(entity.get("numeros", [])):
        score = max(score, 0.95)
    return score


def org_status(top: float, margin: float, payload: Any) -> str:
    if top < payload.floor_threshold:
        return "not_found"
    if top < payload.low_threshold:
        return "low_confidence"
    if top >= payload.accept_threshold and margin >= payload.margin_threshold:
        return "accepted"
    if top >= payload.accept_threshold:
        return "ambiguous"
    return "low_confidence"


def org_candidate_to_json(entity: dict[str, Any], score: float, valid_year: bool) -> dict[str, Any]:
    return {
        "ppn": entity.get("ppn"),
        "label_officiel": entity.get("label_officiel"),
        "type": entity.get("type"),
        "codes": entity.get("codes"),
        "score": round(score, 4),
        "valid_for_year": valid_year,
        "date_debut": entity.get("date_debut"),
        "date_fin": entity.get("date_fin"),
        "url": f"https://www.idref.fr/{entity['ppn']}" if entity.get("ppn") else None,
    }


def org_redirect(index: dict[str, Any], entity: dict[str, Any], year: int | None) -> dict[str, Any] | None:
    """Best match right, wrong era: surface the successor valid that year as evidence.
    Never auto-accepted — a human decides."""
    if year is None:
        return None
    for link in entity.get("successeurs", []):
        node = index["by_ppn"].get(link.get("ppn"))
        target = node or link
        if year_in_window(year, target.get("date_debut"), target.get("date_fin")):
            return {
                "reason": "matched_label_out_of_period",
                "from_ppn": entity.get("ppn"),
                "ppn": target.get("ppn"),
                "label_officiel": target.get("label_officiel") or link.get("label"),
                "date_debut": target.get("date_debut"),
                "date_fin": target.get("date_fin"),
            }
    return None


def remote_org_search(label: str, timeout: float, retries: int, backoff: float, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """Live foaf:Organization search on data.idref.fr, for organizations absent from the
    referential by construction (foreign co-tutelle partners, master's-only institutions)."""
    terms = [token for token in org_tokens(label) if len(token) > 2]
    if not terms:
        return [], "No searchable term in label."
    # un seul filtre, sur le terme le plus long — le plus discriminant (« heidelberg »
    # plutôt que « universitat »). Un ET sur tous les termes est trop strict : la regex
    # SPARQL travaille sur le libellé accentué (« Universität ») alors que nos tokens
    # sont désaccentués. Le tri fin est fait localement par org_similarity.
    discriminants = [t for t in terms if t not in ORG_GENERIC_TERMS] or terms
    filtres = f'FILTER(regex(?label,"{re.escape(max(discriminants, key=len))}","i"))'
    query = (
        "PREFIX foaf: <http://xmlns.com/foaf/0.1/> "
        "SELECT DISTINCT ?s ?label WHERE { "
        "?s a foaf:Organization. ?s skos:prefLabel ?label. "
        f'{filtres} }} LIMIT {max(limit * 10, 50)}'
    )
    url = f"{ORG_SPARQL_ENDPOINT}?{urlencode({'query': query, 'format': 'json'})}"
    # data.idref.fr renvoie 406 sur Accept: application/json malgré ?format=json.
    # l'endpoint SPARQL répond en 30-60 s : le timeout HTTP par défaut (20 s) est
    # calibré pour Qualinka, pas pour lui.
    payload, error = request_json(url, max(timeout, 90.0), retries, backoff,
                                  accept="application/sparql-results+json")
    if error:
        return [], error
    results = []
    for binding in (payload or {}).get("results", {}).get("bindings", []):
        uri = binding.get("s", {}).get("value", "")
        match = re.search(r"idref\.fr/(\w+)/id", uri)
        form = binding.get("label", {}).get("value")
        if match and form:
            results.append({"ppn": match.group(1), "label_officiel": form,
                            "labels": [form], "type": "remote"})
    return results, None


def align_organization(payload: AlignOrganizationRequest) -> dict[str, Any]:
    index = load_org_index()
    year = org_year(payload.year)
    query_numbers = ed_numbers(payload.label)
    pool, scope = org_candidate_pool(index, payload.kind, payload.parent_ppn)

    scored = [
        (score_org_entity(entity, payload.label, query_numbers),
         year_in_window(year, entity.get("date_debut"), entity.get("date_fin")),
         entity)
        for entity in pool
    ]
    # Year-valid entities win as a block; fall back to all of them if none qualifies.
    eligible = [row for row in scored if row[1]] or scored
    eligible.sort(key=lambda row: row[0], reverse=True)

    result: dict[str, Any] = {
        "source": "idref_org_referential",
        "query": {
            "label": payload.label,
            "kind": payload.kind,
            "year": payload.year,
            "parent_ppn": payload.parent_ppn or None,
        },
        "candidate_scope": scope,
        "pool_size": len(pool),
        "status": "not_found",
        "ppn": None,
        "label_officiel": None,
        "score": 0.0,
        "margin": 0.0,
        "redirect": None,
        "candidates": [],
        "error": None,
    }
    if not payload.label.strip():
        result["error"] = "Empty label."
        return result

    if eligible:
        top_score, top_valid, top_entity = eligible[0]
        second_score = eligible[1][0] if len(eligible) > 1 else 0.0
        margin = top_score - second_score
        status = org_status(top_score, margin, payload)
        result.update({
            "status": status,
            "ppn": top_entity.get("ppn") if status == "accepted" else None,
            "label_officiel": top_entity.get("label_officiel") if status == "accepted" else None,
            "score": round(top_score, 4),
            "margin": round(margin, 4),
            "candidates": [
                org_candidate_to_json(entity, score, valid)
                for score, valid, entity in eligible[:payload.max_candidates]
            ],
        })
        if not top_valid and top_score >= payload.low_threshold:
            result["redirect"] = org_redirect(index, top_entity, year)

    if payload.allow_remote and result["status"] in ("not_found", "low_confidence"):
        remote, remote_error = remote_org_search(
            payload.label, payload.timeout, payload.retries, payload.backoff, payload.max_candidates
        )
        remote_scored = sorted(
            ((score_org_entity(entity, payload.label, query_numbers), entity) for entity in remote),
            key=lambda row: row[0],
            reverse=True,
        )[:payload.max_candidates]
        result["remote"] = {
            "source": "idref_sparql",
            "error": remote_error,
            "candidates": [org_candidate_to_json(entity, score, True) for score, entity in remote_scored],
        }
    return result


def align_thesis_organizations(payload: AlignThesisOrganizationsRequest) -> dict[str, Any]:
    """Institution first, then the doctoral school scoped to it — the whole reason the
    composite route exists (~14 candidates instead of ~500)."""
    shared = payload.model_dump(
        include={"year", "allow_remote", "max_candidates", "accept_threshold",
                 "margin_threshold", "low_threshold", "floor_threshold",
                 "timeout", "retries", "backoff"}
    )

    def align_one(label: str, kind: str, parent_ppn: str = "") -> dict[str, Any] | None:
        if not label.strip():
            return None
        return align_organization(
            AlignOrganizationRequest(label=label, kind=kind, parent_ppn=parent_ppn, **shared)
        )

    granting = align_one(payload.granting_institution, "institution")
    parent_ppn = (granting or {}).get("ppn") or ""
    return {
        "source": "idref_org_referential",
        "query": {
            "granting_institution": payload.granting_institution,
            "co_tutelle_institutions": payload.co_tutelle_institutions,
            "doctoral_school": payload.doctoral_school,
            "year": payload.year,
        },
        "granting_institution": granting,
        "co_tutelle_institutions": [
            align_one(label, "institution") for label in payload.co_tutelle_institutions if label.strip()
        ],
        "doctoral_school": align_one(payload.doctoral_school, "doctoral_school", parent_ppn),
    }


@app.get("/org-index/search")
async def org_index_search_endpoint(
    q: str = Query(..., description="Organization label to score against the referential."),
    year: str = Query("", description="Optional defense-year filter."),
    type: str = Query("etablissement", description='"etablissement" or "ecole_doctorale".'),
    parent_ppn: str = Query("", description="Optional institution PPN scoping doctoral schools."),
    max_results: int = Query(DEFAULT_ORG_MAX_CANDIDATES, ge=1, le=50),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    payload = AlignOrganizationRequest(
        label=q,
        kind="doctoral_school" if type == "ecole_doctorale" else "institution",
        year=year,
        parent_ppn=parent_ppn,
        max_candidates=max_results,
    )
    return await run_in_threadpool(align_organization, payload)


@app.post("/align/organization")
async def align_organization_endpoint(
    payload: AlignOrganizationRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await run_in_threadpool(align_organization, payload)


@app.post("/align/thesis-organizations")
async def align_thesis_organizations_endpoint(
    payload: AlignThesisOrganizationsRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await run_in_threadpool(align_thesis_organizations, payload)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8001)
