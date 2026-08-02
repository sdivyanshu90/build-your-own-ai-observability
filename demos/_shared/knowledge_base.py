"""A tiny in-memory knowledge base for the RAG demo.

Deterministic fixture content with a declared version, so the demo produces the
same retrieval results every run and the dataset/knowledge-base lineage in the
UI points at something real.

Cosine similarity over the mock embeddings. Nothing here is a vector database
-- building one is an explicit non-goal -- but the *shape* of the pipeline
(embed, search, rerank, select) is exactly what a real one produces, which is
what the retrieval visualisation is designed around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .mock_provider import MockEmbeddings

__all__ = ["Document", "KnowledgeBase", "KNOWLEDGE_BASE_VERSION", "build_knowledge_base"]

#: Bumped whenever the corpus below changes, so a trace recorded today can be
#: distinguished from one recorded against a re-indexed corpus tomorrow.
KNOWLEDGE_BASE_VERSION = "kb-demo-2026-07"

_CORPUS: tuple[tuple[str, str, str], ...] = (
    (
        "refund-policy",
        "Refund policy",
        "Customers may request a refund within 30 days of delivery, provided the item "
        "is unused and in its original packaging. Refunds are issued to the original "
        "payment method within 5 business days of the returned item being received.",
    ),
    (
        "damaged-items",
        "Damaged items",
        "If an item arrives damaged, photograph the packaging and the item and open a "
        "claim within 48 hours. Damaged items are replaced at no cost and do not count "
        "against the 30 day refund window.",
    ),
    (
        "shipping-timelines",
        "Shipping timelines",
        "Standard shipping takes 3-5 business days. Express delivery arrives the next "
        "business day for orders placed before 14:00. International orders take 7-14 days.",
    ),
    (
        "warranty",
        "Warranty coverage",
        "All hardware carries a 24 month warranty covering manufacturing defects. "
        "Accidental damage is excluded unless the extended protection plan was purchased.",
    ),
    (
        "account-recovery",
        "Account recovery",
        "Reset your password from the sign-in page. Reset links expire after one hour. "
        "If the email does not arrive, check the spam folder before contacting support.",
    ),
    (
        "subscription-tiers",
        "Subscription tiers",
        "The Starter tier includes 10,000 requests per month. Growth includes 250,000 "
        "and priority support. Enterprise adds a dedicated environment and custom pricing.",
    ),
    (
        "data-retention",
        "Data retention",
        "Trace data is retained for 30 days by default and aggregate metrics for 13 "
        "months. Retention can be shortened per project from the settings page.",
    ),
    (
        "returns-process",
        "Returns process",
        "Start a return from Orders, print the prepaid label, and drop the parcel at any "
        "carrier point. Returns are processed within 5 business days of receipt.",
    ),
    # A deliberate near-duplicate of refund-policy. The retrieval diagnostics
    # should flag it, which is the point of including it.
    (
        "refund-policy-copy",
        "Refund policy (regional)",
        "Customers may request a refund within 30 days of delivery, provided the item "
        "is unused and in its original packaging. Refunds are issued to the original "
        "payment method within 5 business days of the returned item being received.",
    ),
)


@dataclass(slots=True)
class Document:
    document_id: str
    title: str
    content: str
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_hit(self, score: float, rank: int) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_id": f"{self.document_id}#0",
            "rank": rank,
            "score": round(score, 6),
            "title": self.title,
            "content": self.content,
            "source": f"https://docs.example.com/{self.document_id}",
            "token_count": max(1, len(self.content) // 4),
            "metadata": dict(self.metadata),
        }


class KnowledgeBase:
    """Cosine-similarity search over a fixed corpus."""

    def __init__(self, documents: Sequence[Document], embeddings: MockEmbeddings) -> None:
        self._documents = list(documents)
        self._embeddings = embeddings

    @property
    def version(self) -> str:
        return KNOWLEDGE_BASE_VERSION

    @property
    def size(self) -> int:
        return len(self._documents)

    def search(self, query_vector: Sequence[float], *, top_k: int = 5) -> list[dict[str, Any]]:
        scored = [
            (document, _cosine(query_vector, document.embedding))
            for document in self._documents
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [
            document.as_hit(score, rank)
            for rank, (document, score) in enumerate(scored[:top_k])
        ]

    def search_empty(self) -> list[dict[str, Any]]:
        """Return no results, for exercising the empty-retrieval path.

        An empty retrieval is one of the most important cases the UI has to
        handle well, and it never occurs by accident in a demo corpus.
        """
        return []


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def build_knowledge_base(embeddings: MockEmbeddings | None = None) -> KnowledgeBase:
    model = embeddings or MockEmbeddings()
    documents = [
        Document(
            document_id=identifier,
            title=title,
            content=content,
            embedding=model.embed(content),
            metadata={"section": identifier.split("-")[0], "language": "en"},
        )
        for identifier, title, content in _CORPUS
    ]
    return KnowledgeBase(documents, model)
