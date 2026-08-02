"""Deterministic mock model provider shared by the demos.

The demos must run with no API keys, no network and no cost, and they must
produce *identical* telemetry on every run so the end-to-end tests can assert
exact token counts and exact prices. A real provider satisfies none of those.

Every response is a pure function of the request, seeded by a hash of the
prompt, so the same question always yields the same answer, the same token
counts and therefore the same cost.

Set ``AIOBS_DEMO_USE_REAL_PROVIDER=1`` plus the relevant provider key to route
through a real model instead; the instrumentation is identical either way,
which is the point.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

__all__ = [
    "MockEmbeddings",
    "MockProvider",
    "MockReranker",
    "using_real_provider",
]


def using_real_provider() -> bool:
    return os.environ.get("AIOBS_DEMO_USE_REAL_PROVIDER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _seed_for(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


@dataclass(slots=True)
class MockResponse:
    """A completion, shaped like an OpenAI response for familiarity."""

    id: str
    model: str
    content: str
    usage: dict[str, int]
    finish_reason: str = "stop"
    system_fingerprint: str = "mock-fp-v1"


_ANSWERS: tuple[str, ...] = (
    "Refunds are available within 30 days of delivery, provided the item is unused "
    "and in its original packaging. Start the process from Orders → Request refund.",
    "Standard shipping takes 3-5 business days. Express delivery arrives the next "
    "business day when ordered before 14:00.",
    "Your warranty covers manufacturing defects for 24 months. Accidental damage is "
    "not included unless you purchased the extended plan.",
    "You can reset your password from the sign-in page. If the reset email does not "
    "arrive within ten minutes, check your spam folder before contacting support.",
)


@dataclass(slots=True)
class MockProvider:
    """A deterministic chat model."""

    model: str = "mock-model-v1"
    provider: str = "mock"
    #: Milliseconds of simulated latency per generated token.
    latency_per_token_ms: float = 0.4
    #: Fraction of calls that fail, so the demos exercise the error path.
    failure_rate: float = 0.0
    #: Fraction of calls served from a simulated prompt cache.
    cache_hit_rate: float = 0.0

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> MockResponse:
        """Return a deterministic completion for ``messages``."""
        prompt = "\n".join(str(message.get("content", "")) for message in messages)
        rng = random.Random(_seed_for(prompt))

        if self.failure_rate and rng.random() < self.failure_rate:
            raise ProviderError(
                f"{self.provider} returned 529 overloaded", status_code=529, retryable=True
            )

        content = _ANSWERS[rng.randrange(len(_ANSWERS))]
        input_tokens = max(1, len(prompt) // 4)
        output_tokens = max(1, len(content) // 4)
        cached = int(input_tokens * 0.4) if rng.random() < self.cache_hit_rate else 0

        # Simulated latency, so the waterfall in the UI has realistic shape.
        time.sleep(min(output_tokens * self.latency_per_token_ms / 1000, 0.4))

        return MockResponse(
            id=f"mock-{_seed_for(prompt):08x}",
            model=self.model,
            content=content,
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cached_tokens": cached,
            },
        )

    def stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 512,
        cancel_after: int | None = None,
    ) -> Iterator[str]:
        """Yield the completion word by word.

        ``cancel_after`` stops early, which is how the demos exercise the
        streaming-cancellation path -- a case that is easy to get wrong and
        produces a span with no end time if the instrumentation is sloppy.
        """
        response = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        words = response.content.split(" ")
        for index, word in enumerate(words):
            if cancel_after is not None and index >= cancel_after:
                return
            time.sleep(self.latency_per_token_ms / 1000)
            yield word + (" " if index < len(words) - 1 else "")


class ProviderError(RuntimeError):
    """A simulated provider failure."""

    def __init__(self, message: str, *, status_code: int = 500, retryable: bool = True) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(slots=True)
class MockEmbeddings:
    """Deterministic embeddings: a hash-seeded unit vector per text."""

    model: str = "mock-embedding-v1"
    dimensions: int = 64

    def embed(self, text: str) -> list[float]:
        rng = random.Random(_seed_for(text))
        vector = [rng.gauss(0, 1) for _ in range(self.dimensions)]
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


@dataclass(slots=True)
class MockReranker:
    """Deterministic reranker that reorders by a hash-seeded score.

    Deliberately *not* the identity function: a reranker that never changes the
    order would make the rank-movement view in the UI permanently empty, and
    that view is one of the more useful retrieval diagnostics.
    """

    model: str = "mock-reranker-v1"
    failure_rate: float = 0.0
    _calls: int = field(default=0)

    def rerank(
        self, query: str, documents: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._calls += 1
        rng = random.Random(_seed_for(query) + self._calls)
        if self.failure_rate and rng.random() < self.failure_rate:
            raise ProviderError("reranker unavailable", status_code=503, retryable=True)
        scored = []
        for document in documents:
            seed = _seed_for(query + str(document.get("document_id", "")))
            scored.append({**document, "rerank_score": (seed % 1000) / 1000})
        scored.sort(key=lambda item: item["rerank_score"], reverse=True)
        for position, document in enumerate(scored):
            document["rerank_rank"] = position
        return scored
