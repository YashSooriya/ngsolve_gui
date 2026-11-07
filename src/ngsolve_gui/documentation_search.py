"""Lazy, offline search over the version-matched local Sphinx site."""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
import unicodedata
from urllib.parse import quote


INDEX_NAME = "search-index.json"
INDEX_SCHEMA = "lorentz.documentation-search"
INDEX_SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_INDEX_ENTRIES = 100_000
MAX_RESULTS = 5
_TOKEN = re.compile(r"[^\W_]+(?:_[^\W_]+)*", re.UNICODE)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "what",
    "when", "where", "with",
}
_WORKFLOW_INTENT = {
    "assignment", "create", "entry", "error", "errors", "failure", "failed",
    "gui", "help", "import", "interface", "missing", "open", "recover",
    "recovery", "reuse", "save", "select", "setup", "stale", "troubleshoot",
    "troubleshooting", "workflow",
}


class DocumentationSearchError(ValueError):
    """The local documentation search index cannot safely be used."""


def tokens(value: str) -> list[str]:
    """Normalize punctuation, case, accents and subscript-style underscores."""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return [token.replace("_", "") for token in _TOKEN.findall(normalized)]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()

    def handle_starttag(self, _tag, attrs):
        value = dict(attrs).get("id")
        if value:
            self.ids.add(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class DocumentationSearch:
    """Search the shipped index on demand, then cache its parsed corpus."""

    def __init__(self, site_root: str | Path, expected_version: str):
        self.site_root = Path(site_root).resolve(strict=True)
        if not self.site_root.is_dir():
            raise DocumentationSearchError("The local documentation root is not a directory.")
        self.expected_version = str(expected_version).strip()
        self.index_path = self.site_root / INDEX_NAME
        self._entries: list[dict] | None = None
        self._anchors_by_page: dict[str, set[str]] = {}
        self.last_load_seconds = 0.0
        self.last_search_seconds = 0.0
        self.index_bytes = 0
        self.index_version: str | None = None
        self.skipped_entries = 0

    @property
    def loaded(self) -> bool:
        return self._entries is not None

    @property
    def entry_count(self) -> int:
        return len(self._entries or ())

    def _load(self) -> None:
        if self._entries is not None:
            return
        started = time.perf_counter()
        try:
            resolved_index = self.index_path.resolve(strict=True)
            if not _inside(self.site_root, resolved_index) or not resolved_index.is_file():
                raise DocumentationSearchError("The documentation search index is missing.")
            self.index_bytes = resolved_index.stat().st_size
            if self.index_bytes > MAX_INDEX_BYTES:
                raise DocumentationSearchError("The documentation search index is too large.")
            payload = json.loads(resolved_index.read_text(encoding="utf-8"))
        except DocumentationSearchError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DocumentationSearchError("The documentation search index is missing or invalid.") from error

        if not isinstance(payload, dict):
            raise DocumentationSearchError("The documentation search index has an invalid format.")
        if payload.get("schema") != INDEX_SCHEMA or payload.get("schema_version") != INDEX_SCHEMA_VERSION:
            raise DocumentationSearchError("The documentation search index uses an unsupported format.")
        version = str(payload.get("version", "")).strip()
        self.index_version = version or None
        if not version or version != self.expected_version:
            raise DocumentationSearchError(
                "The documentation search index does not match this LORENTZ version."
            )
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) > MAX_INDEX_ENTRIES:
            raise DocumentationSearchError("The documentation search index has invalid section data.")

        valid_pages: dict[str, Path | None] = {}
        entries = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                self.skipped_entries += 1
                continue
            title = _clean_text(raw.get("title", ""))
            page_title = _clean_text(raw.get("page_title", ""))
            page = raw.get("page")
            fragment = raw.get("fragment")
            text = _clean_text(raw.get("text", ""))
            if (
                not title or len(title) > 300
                or not isinstance(page_title, str) or len(page_title) > 500
                or not isinstance(page, str) or len(page) > 700
                or not isinstance(fragment, str) or not fragment or len(fragment) > 500
                or any(char in fragment for char in "\r\n\t\"'<>#")
                or not isinstance(raw.get("text", ""), str)
            ):
                self.skipped_entries += 1
                continue
            relative = PurePosixPath(page)
            if (
                relative.is_absolute() or ".." in relative.parts
                or "\\" in page or ":" in page or relative.suffix.casefold() != ".html"
            ):
                self.skipped_entries += 1
                continue
            if page not in valid_pages:
                try:
                    resolved_page = (self.site_root / Path(*relative.parts)).resolve(strict=True)
                    valid_pages[page] = (
                        resolved_page
                        if _inside(self.site_root, resolved_page) and resolved_page.is_file()
                        else None
                    )
                except OSError:
                    valid_pages[page] = None
            if valid_pages[page] is None:
                self.skipped_entries += 1
                continue

            title_terms = tokens(title)
            context_terms = tokens(page_title)
            text_terms = tokens(text[:12000])
            title_tokens = Counter(title_terms)
            context_tokens = Counter(context_terms)
            text_tokens = Counter(text_terms)
            all_tokens = set(title_tokens) | set(context_tokens) | set(text_tokens)
            entries.append({
                "title": title,
                "page_title": page_title,
                "page": relative.as_posix(),
                "fragment": fragment,
                "text": text[:12000],
                "title_phrase": " ".join(title_terms),
                "context_phrase": " ".join(context_terms),
                "text_phrase": " ".join(text_terms),
                "title_tokens": title_tokens,
                "context_tokens": context_tokens,
                "text_tokens": text_tokens,
                "all_tokens": all_tokens,
            })
        if not entries:
            raise DocumentationSearchError("The documentation search index contains no usable sections.")
        self._entries = entries
        self.last_load_seconds = time.perf_counter() - started

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        query_tokens = [token for token in tokens(query) if token not in _STOPWORDS]
        if not query_tokens:
            self.last_search_seconds = 0.0
            return []
        self._load()
        started = time.perf_counter()
        assert self._entries is not None
        query_unique = list(dict.fromkeys(query_tokens))
        document_frequency = Counter(
            token for entry in self._entries for token in entry["all_tokens"]
        )
        count = len(self._entries)
        phrase = " ".join(query_tokens)
        workflow_query = bool(set(query_tokens) & _WORKFLOW_INTENT)
        ranked = []
        for entry in self._entries:
            matched = [token for token in query_unique if token in entry["all_tokens"]]
            if not matched:
                continue
            score = 0.0
            for token in matched:
                frequency = document_frequency[token]
                idf = math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
                title_tf = entry["title_tokens"].get(token, 0)
                context_tf = entry["context_tokens"].get(token, 0)
                text_tf = entry["text_tokens"].get(token, 0)
                score += idf * (
                    9.0 * (1.0 + math.log1p(title_tf))
                    + 2.5 * (1.0 + math.log1p(context_tf))
                    + 1.0 * (1.0 + math.log1p(text_tf))
                )
            coverage = len(matched) / len(query_unique)
            score *= coverage * coverage
            if phrase in entry["title_phrase"]:
                score *= 1.75
            elif phrase in entry["context_phrase"]:
                score *= 1.2
            elif phrase and phrase in entry["text_phrase"]:
                score *= 1.08

            path_lower = entry["page"].casefold()
            if "user_guide/" in path_lower:
                score *= 1.1
            if workflow_query:
                if "user_guide/" in path_lower:
                    score *= 1.24
                elif any(part in path_lower for part in ("advanced_reference/", "tutorials/notebooks/")):
                    score *= 0.86

            result = {
                key: entry[key]
                for key in ("title", "page_title", "page", "fragment", "text")
            }
            result["excerpt"] = self._excerpt(entry["text"], query_unique)
            result["score"] = score
            ranked.append(result)

        ranked.sort(key=lambda result: (-result["score"], result["title"].casefold(), result["page"]))
        deduplicated = []
        seen_titles = set()
        for result in ranked:
            title_key = " ".join(tokens(result["title"]))
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            deduplicated.append(result)
            if len(deduplicated) >= min(MAX_RESULTS, max(1, int(limit))):
                break
        self.last_search_seconds = time.perf_counter() - started
        return deduplicated

    @staticmethod
    def _excerpt(text: str, query_tokens: list[str], max_chars: int = 240) -> str:
        text = _clean_text(text)
        if not text:
            return ""
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|(?<=;)\s+", text) if part.strip()]
        if not sentences:
            sentences = [text]
        def relevance(sentence):
            words = set(tokens(sentence))
            return (len(words.intersection(query_tokens)), -abs(len(sentence) - max_chars))
        excerpt = max(sentences, key=relevance)
        if len(excerpt) <= max_chars:
            return excerpt
        excerpt = excerpt[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
        return excerpt + "…" if excerpt else text[:max_chars - 1] + "…"

    def local_url(self, result: dict) -> str:
        """Validate a result against the installed site and return its file URL."""
        self._load()
        if not isinstance(result, dict):
            raise DocumentationSearchError("The selected documentation result is invalid.")
        page = result.get("page")
        fragment = result.get("fragment")
        relative = PurePosixPath(page) if isinstance(page, str) else PurePosixPath("")
        if (
            not page or not isinstance(fragment, str) or not fragment
            or relative.is_absolute() or ".." in relative.parts
            or "\\" in str(page) or ":" in str(page)
            or relative.suffix.casefold() != ".html"
            or any(char in fragment for char in "\r\n\t\"'<>#")
        ):
            raise DocumentationSearchError("The selected documentation path or heading is invalid.")
        page_path = (self.site_root / Path(*relative.parts)).resolve(strict=True)
        if not _inside(self.site_root, page_path) or not page_path.is_file():
            raise DocumentationSearchError("The selected documentation page is outside the local site.")
        key = relative.as_posix()
        if key not in self._anchors_by_page:
            parser = _AnchorParser()
            try:
                with page_path.open("r", encoding="utf-8") as source:
                    while chunk := source.read(65536):
                        parser.feed(chunk)
            except (OSError, UnicodeError) as error:
                raise DocumentationSearchError("The selected documentation page cannot be read.") from error
            self._anchors_by_page[key] = parser.ids
        if fragment not in self._anchors_by_page[key]:
            raise DocumentationSearchError("The selected documentation heading is no longer available.")
        return page_path.as_uri() + "#" + quote(fragment, safe="-._~:")
