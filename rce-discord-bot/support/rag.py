"""Knowledge base retrieval (RAG) for the support AI and the AI chat.

Instead of putting the whole knowledge file into every prompt, the file is split into sections (by its Markdown
headings and paragraphs) and only the sections that match the question are sent: the AI gets less to read, a big
knowledge base no longer gets cut off at a size limit, and old unrelated facts can't confuse an answer.

Search: BM25 (the classic ranking of search engines) over the words of each section, its heading counting double,
with light stemming ("wipes" = "wipe") and typo tolerance (an unknown word matches known words with similar letter
triples, "wipee" -> "wipe"). Pure Python – no extra packages, no embedding API (DeepSeek has none), nothing leaves the
PC. The AI also gets a tool to search the knowledge base itself (search_knowledge_base), so it can look something up
in the knowledge base's own language when the first pick missed it.

A knowledge base that fits into the budget is sent whole, exactly as before.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

CHUNK_CHARS = 900            # a section is split into pieces of about this size
K1, B = 1.5, 0.75            # BM25 parameters (the usual values)
FUZZY_MIN = 0.5              # letter-triple similarity for a typo to count
FUZZY_WEIGHT = 0.6           # a typo match counts this much of an exact one
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
WORD = re.compile(r"\w+", re.UNICODE)
# Small words that say nothing about the topic (English and German – the languages knowledge bases come in)
STOP = frozenset("""
a an and are as at be but by can do does for from has have how i if in is it its me my no not of on or our so that
the their them there this to was we what when where which who why will with you your
aber als am an auch auf aus bei bin bis da das dass dem den der des die du ein eine einem einen einer es fur für hab
habe hat ich ihr im in ist ja kann man mein mit nach nicht noch nur oder sich sie sind so um und uns von vom was wie
wir wo zu zum zur
""".split())
_SUFFIXES = ("ings", "ing", "edly", "ed", "ies", "es", "s", "en", "er", "e")


def _fold(text: str) -> str:
    """Lower case without accents (é -> e, ü -> u) – "Bäse" and "base" are found alike."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _stem(word: str) -> str:
    if len(word) > 4 and not word.isdigit():
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in WORD.findall(_fold(text)) if w not in STOP and (len(w) > 1 or w.isdigit())]


def _trigrams(word: str) -> set[str]:
    padded = f"  {word} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


@dataclass(slots=True)
class Chunk:
    id: int
    heading: str             # "Wipes › Schedule" – the headings above this piece
    text: str

    def render(self) -> str:
        return f"[{self.heading}]\n{self.text}" if self.heading else self.text


def split(text: str, max_chars: int = CHUNK_CHARS) -> list[Chunk]:
    """The knowledge file in pieces: one per heading section, long sections cut at paragraphs (then lines)."""
    chunks: list[Chunk] = []
    path: list[tuple[int, str]] = []
    paragraphs: list[str] = []
    current: list[str] = []

    def heading() -> str:
        return " › ".join(title for _, title in path)

    def flush_paragraph() -> None:
        if current:
            paragraphs.append("\n".join(current).strip())
            current.clear()

    def flush_section() -> None:
        flush_paragraph()
        piece = ""
        for para in (p for p in paragraphs if p):
            parts = [para] if len(para) <= max_chars else _cut(para, max_chars)
            for part in parts:
                if piece and len(piece) + len(part) + 2 > max_chars:
                    chunks.append(Chunk(len(chunks), heading(), piece))
                    piece = ""
                piece = f"{piece}\n\n{part}" if piece else part
        if piece:
            chunks.append(Chunk(len(chunks), heading(), piece))
        paragraphs.clear()

    for line in (text or "").splitlines():
        m = HEADING.match(line.strip())
        if m:
            flush_section()
            level = len(m[1])
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m[2].strip()))
        elif not line.strip():
            flush_paragraph()
        else:
            current.append(line.rstrip())
    flush_section()
    return chunks


def _cut(text: str, max_chars: int) -> list[str]:
    """A paragraph longer than a piece: by lines, then by sentences, then hard."""
    out, piece = [], ""
    units = text.splitlines() if "\n" in text else re.split(r"(?<=[.!?])\s+", text)
    for unit in units:
        while len(unit) > max_chars:
            out.append(unit[:max_chars])
            unit = unit[max_chars:]
        if piece and len(piece) + len(unit) + 1 > max_chars:
            out.append(piece)
            piece = ""
        piece = f"{piece}\n{unit}" if piece else unit
    if piece:
        out.append(piece)
    return out


@dataclass
class KnowledgeIndex:
    text: str
    chunks: list[Chunk] = field(init=False)
    _tf: list[Counter] = field(init=False, repr=False)
    _len: list[int] = field(init=False, repr=False)
    _df: Counter = field(init=False, repr=False)
    _grams: dict[str, set[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.chunks = split(self.text)
        self._tf, self._len, self._df = [], [], Counter()
        for chunk in self.chunks:
            words = tokens(chunk.text) + tokens(chunk.heading) * 2          # the heading counts double
            tf = Counter(words)
            self._tf.append(tf)
            self._len.append(len(words) or 1)
            self._df.update(tf.keys())
        self._avg = sum(self._len) / len(self._len) if self._len else 1.0
        self._grams = {w: _trigrams(w) for w in self._df if not w.isdigit()}

    def _idf(self, term: str) -> float:
        n, df = len(self.chunks), self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _expand(self, term: str) -> list[tuple[str, float]]:
        """The term itself if the knowledge base has it, else up to two similar words (typos)."""
        if term in self._df:
            return [(term, 1.0)]
        if term.isdigit() or len(term) < 4:
            return []
        grams = _trigrams(term)
        near = sorted(((len(grams & g) / len(grams | g), w) for w, g in self._grams.items()), reverse=True)[:2]
        return [(w, FUZZY_WEIGHT * sim) for sim, w in near if sim >= FUZZY_MIN]

    def search(self, query: str, k: int = 6) -> list[tuple[float, Chunk]]:
        """The k best matching pieces, best first (only pieces that match at all)."""
        terms: dict[str, float] = {}
        for term in tokens(query):
            for word, weight in self._expand(term):
                terms[word] = max(terms.get(word, 0.0), weight)
        scores = []
        for i, tf in enumerate(self._tf):
            score = 0.0
            for term, weight in terms.items():
                f = tf.get(term)
                if f:
                    norm = f * (K1 + 1) / (f + K1 * (1 - B + B * self._len[i] / self._avg))
                    score += weight * self._idf(term) * norm
            if score > 0:
                scores.append((score, self.chunks[i]))
        scores.sort(key=lambda s: (-s[0], s[1].id))
        return scores[:k]

    def select(self, query: str, budget: int, k: int = 8, *, tool: bool = True) -> tuple[str, dict]:
        """The knowledge for one answer: all of it if it fits into `budget` characters, else the best matching
        pieces (plus the first one – usually the server's basics – when there's room), in the file's order.
        Returns (text, info) – info for the footer/log: {"mode", "used", "total"}."""
        if len(self.text) <= budget or not self.chunks:
            return self.text, {"mode": "all", "used": len(self.chunks), "total": len(self.chunks)}
        picked: list[Chunk] = []
        size = 0
        for _, chunk in self.search(query, k):
            if size + len(chunk.render()) + 2 > budget:
                continue
            picked.append(chunk)
            size += len(chunk.render()) + 2
        intro = self.chunks[0]
        if intro not in picked and size + len(intro.render()) + 2 <= budget:
            picked.append(intro)
        picked.sort(key=lambda c: c.id)
        note = ("(Excerpt: only the sections of the knowledge base that match this conversation. "
                + ("If the answer isn't here, search it with search_knowledge_base before saying you don't know.)"
                   if tool else "If the answer isn't here, say you don't know – don't guess.)"))
        text = note + "\n\n" + "\n\n".join(c.render() for c in picked)
        return text, {"mode": "rag", "used": len(picked), "total": len(self.chunks)}

    def lookup(self, query: str, k: int = 4) -> list[dict]:
        """For the AI's search tool: the best pieces with their headings."""
        return [{"section": c.heading or "(top)", "text": c.text, "score": round(s, 2)}
                for s, c in self.search(query, k)]


SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_knowledge_base",
    "description": "Searches the server's knowledge base (rules, wipes, kits, prices, how things work …) and returns "
                   "the best matching sections. Use it when the knowledge excerpt in your instructions doesn't answer "
                   "the question – try the words the knowledge base would use (its language, short keywords).",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to look for, as short keywords."}},
        "required": ["query"]}}}
