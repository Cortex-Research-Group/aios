"""Persistent memory.

Plain markdown files, one fact per file, searched with a small TF-IDF ranker.
No vector database and no embedding model: those cost a pip dependency and
hundreds of megabytes on a boot medium, and for a few thousand personal notes
keyword ranking is honestly competitive. The files stay greppable and
human-editable, which matters when your OS's memory is on a stick you own.
"""

import json
import math
import re
import time
from pathlib import Path

from . import paths

WORD = re.compile(r"[a-z0-9]+")
STOP = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "to",
    "of", "in", "on", "at", "for", "with", "it", "this", "that", "i", "my", "me",
}


def _tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s or "note")[:60]


class Memory:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.MEMORY
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, title: str, content: str, tags: list[str] | None = None) -> Path:
        """Store a fact. Re-writing the same title updates it in place."""
        path = self.root / f"{_slug(title)}.md"
        meta = {"title": title, "tags": tags or [], "updated": time.strftime("%Y-%m-%d %H:%M")}
        path.write_text(
            f"---\n{json.dumps(meta)}\n---\n\n{content.strip()}\n", encoding="utf-8"
        )
        return path

    def _parse(self, path: Path) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = {}, raw
        if raw.startswith("---\n"):
            end = raw.find("\n---", 4)
            if end != -1:
                try:
                    meta = json.loads(raw[4:end])
                except json.JSONDecodeError:
                    meta = {}
                body = raw[end + 4 :]
        return {
            "path": path,
            "title": meta.get("title", path.stem),
            "tags": meta.get("tags", []),
            "updated": meta.get("updated", ""),
            "content": body.strip(),
        }

    def all(self) -> list[dict]:
        return [n for p in sorted(self.root.glob("*.md")) if (n := self._parse(p))]

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Rank notes against the query. Title and tag hits weigh more than body."""
        terms = _tokens(query)
        if not terms:
            return []

        notes = self.all()
        if not notes:
            return []

        # Document frequency, for IDF.
        df: dict[str, int] = {}
        docs = []
        for note in notes:
            body = _tokens(note["content"])
            head = _tokens(note["title"] + " " + " ".join(note["tags"]))
            docs.append((note, body, set(body) | set(head)))
            for term in set(body) | set(head):
                df[term] = df.get(term, 0) + 1

        n = len(notes)
        scored = []
        for note, body, vocab in docs:
            score = 0.0
            head = _tokens(note["title"] + " " + " ".join(note["tags"]))
            for term in terms:
                if term not in vocab:
                    continue
                idf = math.log((n + 1) / (df.get(term, 0) + 1)) + 1
                tf = body.count(term) + 3 * head.count(term)  # title match dominates
                score += idf * (1 + math.log(tf)) if tf else 0
            if score > 0:
                scored.append((score, note))

        scored.sort(key=lambda x: (-x[0], x[1]["title"]))
        return [{**note, "score": round(score, 3)} for score, note in scored[:limit]]

    def forget(self, title: str) -> bool:
        path = self.root / f"{_slug(title)}.md"
        if path.exists():
            path.unlink()
            return True
        return False
