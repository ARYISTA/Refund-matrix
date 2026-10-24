"""Embedding contracts.

HashEmbeddingProvider is deterministic and dependency-free for tests and local smoke runs.
Production can replace it with the configured model without changing ingestion or retrieval.
"""

import hashlib
import math
import re
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    dimension: int

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a normalized vector of exactly ``dimension`` values."""


class HashEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimension: int = 384) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        tokens = re.findall(r"\w+", text.casefold())
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            values[index] += sign
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values
