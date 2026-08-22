"""Tiered model use: the cheapest model that can do each job.

Three tiers, all routed through the gateway under the same scope:

- **Gemma** (open weights) triages uploaded documents into legal / financial /
  corporate / other. High volume, low judgment, no reason to spend a frontier
  model on it - and previously this was crude keyword scoring.
- **Gemini embeddings** detect risk clauses semantically. Substring matching only
  catches a clause phrased the canonical way; "the Customer may end this agreement
  if ownership of the Supplier changes" never contains "change of control".
- **Gemini 3.5** (in `orchestrator`) does the reasoning, debate, and verdict.

Every function degrades to a deterministic result, so an outage or a missing
credential downgrades quality rather than breaking the audit.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

TRIAGE_MODEL = os.getenv("GEMMA_MODEL", "gemma-4-26b-a4b-it")
EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

DOCUMENT_CLASSES = ("legal", "financial", "corporate", "other")

# Canonical phrasings of the clauses that matter in an acquisition. Documents are
# compared against these semantically, so paraphrases are still caught.
CANONICAL_CLAUSES: dict[str, str] = {
    "Change of Control": (
        "the counterparty may terminate or renegotiate the agreement if ownership or "
        "control of the company changes, such as on an acquisition or merger"
    ),
    "Uncapped Indemnity": (
        "one party indemnifies the other without any financial cap or limitation of liability"
    ),
    "Termination for Convenience": (
        "either party may terminate the agreement at will on notice, without cause"
    ),
    "Assignment Restriction": (
        "the agreement may not be assigned or transferred without the prior written consent "
        "of the other party"
    ),
    "Exclusivity": (
        "the supplier is prohibited from providing similar services to other customers, or the "
        "customer must buy exclusively from this supplier"
    ),
    "Most Favoured Nation": (
        "the customer is entitled to the best pricing offered to any other customer"
    ),
    "Auto-Renewal": (
        "the agreement renews automatically for a further term unless notice is served"
    ),
}

SIMILARITY_THRESHOLD = float(os.getenv("FLEET_CLAUSE_SIMILARITY", "0.62"))
# The winning label must beat the next one by this much, or the text is ambiguous.
MARGIN_THRESHOLD = float(os.getenv("FLEET_CLAUSE_MARGIN", "0.04"))
MAX_SEGMENTS_PER_DOCUMENT = int(os.getenv("FLEET_MAX_CLAUSE_SEGMENTS", "40"))

_EMBEDDING_CACHE: dict[str, list[float]] = {}


def _api_client():
    """Gemma and the embedding models are served by the Gemini API.

    Production runs frontier reasoning on Vertex AI, but open models are reached
    with the API key, so this deliberately does not use the Vertex client.
    """

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    from google import genai

    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Tier 1: document triage with Gemma
# ---------------------------------------------------------------------------


def _keyword_classification(name: str, text: str) -> str:
    """Deterministic fallback, used when no model is available."""

    import data_room_loader

    return data_room_loader.classify_document(name, text)


def classify_documents(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify each document with Gemma in one call. Gateway tool handler."""

    if not documents:
        return {"status": "empty", "model": None, "classifications": []}

    client = _api_client()
    if client is None:
        return {
            "status": "fallback",
            "model": "keyword-heuristic",
            "classifications": [
                {
                    "file_name": document.get("file_name", ""),
                    "classification": _keyword_classification(
                        document.get("file_name", ""), document.get("text_excerpt", "")
                    ),
                    "rationale": "No model credentials; classified by keyword heuristic.",
                }
                for document in documents
            ],
        }

    listing = "\n\n".join(
        f"[{index}] FILE: {document.get('file_name', 'unknown')}\n"
        f"EXCERPT: {str(document.get('text_excerpt', ''))[:1200]}"
        for index, document in enumerate(documents)
    )
    prompt = (
        "You triage documents in an M&A data room. For each document below, choose exactly "
        f"one class from {list(DOCUMENT_CLASSES)} and give a short reason.\n\n"
        "Respond with JSON only, as a list of objects with keys index, classification, "
        "rationale. No prose, no code fences.\n\n"
        f"{listing}"
    )

    try:
        response = client.models.generate_content(model=TRIAGE_MODEL, contents=prompt)
        text = re.sub(r"^```(?:json)?|```$", "", (response.text or "").strip(), flags=re.M).strip()
        parsed = json.loads(text)
        classifications = []
        for entry in parsed if isinstance(parsed, list) else []:
            index = int(entry.get("index", -1))
            if not 0 <= index < len(documents):
                continue
            label = str(entry.get("classification", "other")).lower().strip()
            classifications.append(
                {
                    "file_name": documents[index].get("file_name", ""),
                    "classification": label if label in DOCUMENT_CLASSES else "other",
                    "rationale": str(entry.get("rationale", ""))[:300],
                }
            )
        if classifications:
            return {"status": "success", "model": TRIAGE_MODEL, "classifications": classifications}
    except Exception as exc:
        return {
            "status": "error",
            "model": TRIAGE_MODEL,
            "error": f"{exc.__class__.__name__}",
            "classifications": [
                {
                    "file_name": document.get("file_name", ""),
                    "classification": _keyword_classification(
                        document.get("file_name", ""), document.get("text_excerpt", "")
                    ),
                    "rationale": "Triage model failed; classified by keyword heuristic.",
                }
                for document in documents
            ],
        }

    return {"status": "empty_response", "model": TRIAGE_MODEL, "classifications": []}


# ---------------------------------------------------------------------------
# Tier 2: semantic clause detection with embeddings
# ---------------------------------------------------------------------------


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


MIN_SEGMENT_CHARS = 60
MIN_SEGMENT_WORDS = 10


def _segments(text: str) -> list[str]:
    """Split a document into clause-sized pieces worth embedding.

    Titles and headings are excluded: they carry the document's general topic, so
    they resemble every clause a little and none of them specifically.
    """

    pieces = re.split(r"(?<=[.;])\s+|\n{2,}", text or "")
    segments = [" ".join(piece.split()) for piece in pieces]
    return [
        segment
        for segment in segments
        if len(segment) >= MIN_SEGMENT_CHARS and len(segment.split()) >= MIN_SEGMENT_WORDS
    ][:MAX_SEGMENTS_PER_DOCUMENT]


def _embed(client: Any, texts: list[str]) -> list[list[float]]:
    response = client.models.embed_content(model=EMBEDDING_MODEL, contents=texts)
    return [list(item.values) for item in response.embeddings]


def semantic_clause_scan(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Find risk clauses by meaning rather than wording. Gateway tool handler."""

    client = _api_client()
    if client is None or not documents:
        return {"status": "unavailable", "model": None, "matches": []}

    try:
        missing = [label for label in CANONICAL_CLAUSES if label not in _EMBEDDING_CACHE]
        if missing:
            vectors = _embed(client, [CANONICAL_CLAUSES[label] for label in missing])
            _EMBEDDING_CACHE.update(dict(zip(missing, vectors)))

        matches: dict[tuple[str, str], dict[str, Any]] = {}
        for document in documents:
            segments = _segments(str(document.get("text_excerpt", "")))
            if not segments:
                continue
            segment_vectors = _embed(client, segments)

            for segment, vector in zip(segments, segment_vectors):
                # A clause is whatever it most resembles. Scoring every label
                # independently lets one sentence match half the taxonomy, which is
                # how an indemnity clause ends up reported as exclusivity.
                scored = sorted(
                    ((label, _cosine(_EMBEDDING_CACHE[label], vector)) for label in CANONICAL_CLAUSES),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
                label, score = scored[0]
                runner_up = scored[1][1] if len(scored) > 1 else 0.0
                # Require both an absolute match and a clear win over the next label,
                # so genuinely ambiguous text is reported as nothing rather than wrongly.
                if score < SIMILARITY_THRESHOLD or (score - runner_up) < MARGIN_THRESHOLD:
                    continue

                key = (document.get("file_name", ""), label)
                if key not in matches or score > matches[key]["similarity"]:
                    matches[key] = {
                        "file_name": document.get("file_name", ""),
                        "clause": label,
                        "similarity": round(score, 3),
                        "margin": round(score - runner_up, 3),
                        "excerpt": segment[:220],
                    }

        return {
            "status": "success",
            "model": EMBEDDING_MODEL,
            "matches": sorted(matches.values(), key=lambda item: item["similarity"], reverse=True),
        }
    except Exception as exc:
        return {"status": "error", "model": EMBEDDING_MODEL, "error": exc.__class__.__name__, "matches": []}
