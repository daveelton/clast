"""BM25 keyword search over indexed symbols.

Builds a search corpus from symbol names, signatures, doc comments,
and parent context. Ranks results by relevance.
"""

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from .db import ASTDatabase, Symbol


@dataclass
class SearchResult:
    symbol: Symbol
    score: float


def _tokenize(text: str) -> list[str]:
    """Split text into searchable tokens.

    Handles camelCase, snake_case, C++ qualified names, and natural language.
    """
    if not text:
        return []

    # Split on :: and common separators
    text = text.replace("::", " ").replace("->", " ").replace(".", " ")

    # Split camelCase: "processBlock" -> "process Block"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split on underscores
    text = text.replace("_", " ")

    # Lowercase and split on whitespace/punctuation
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())

    return [t for t in tokens if len(t) > 1]  # drop single chars


def _build_document(sym: Symbol) -> str:
    """Build a searchable document string from a symbol."""
    parts = [
        sym.qualified_name,
        sym.name,
        sym.signature,
        sym.doc,
        sym.kind,
        sym.parent_name,
    ]
    # Include parameter names and types
    for param in sym.parameters:
        parts.append(param.get("name", ""))
        parts.append(param.get("type", ""))
    # Include base class names
    for base in sym.base_names:
        parts.append(base)
    return " ".join(p for p in parts if p)


class SymbolSearch:
    """BM25-based search over the symbol index."""

    def __init__(self, db: ASTDatabase):
        self.db = db
        self._corpus: list[list[str]] = []
        self._symbols: list[Symbol] = []
        self._bm25: BM25Okapi | None = None

    def build_index(self):
        """Load all symbols and build the BM25 index."""
        self._symbols = self.db.get_all_symbols()
        self._corpus = [_tokenize(_build_document(sym)) for sym in self._symbols]

        if self._corpus:
            self._bm25 = BM25Okapi(self._corpus)
        else:
            self._bm25 = None

    def search(self, query: str, limit: int = 10, kinds: list[str] | None = None) -> list[SearchResult]:
        """Search symbols by keyword query.

        Args:
            query: Natural language or keyword search query
            limit: Maximum results to return
            kinds: Optional filter by symbol kind (e.g. ["method", "function"])

        Returns:
            Ranked list of SearchResult
        """
        if not self._bm25 or not self._symbols:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)

        # Pair symbols with scores, filter by kind if specified
        results = []
        for i, (sym, score) in enumerate(zip(self._symbols, scores)):
            if score <= 0:
                continue
            if kinds and sym.kind not in kinds:
                continue
            results.append(SearchResult(symbol=sym, score=float(score)))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]
