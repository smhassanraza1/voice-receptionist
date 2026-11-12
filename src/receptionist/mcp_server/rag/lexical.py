"""Lexical retrieval implemented from scratch: Okapi BM25, character n-gram TF-IDF cosine, and
the term-coverage score used as a confidence signal.

BM25 rewards (stemmed) term matches and falls back to the closest indexed term for unknown
words. The character n-gram ranker compares overlapping 3-5 character pieces of each word, so
it still matches when speech recognition mangles words ("presciption refil").
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

# Small English stopword list: function words plus filler common in spoken questions.
_STOPWORD_TEXT = """
a about all also am an and any anything are as at be been being but by can could did do does
doing for from get got had has have having he her here hi him his how i if in into is it its
just let like me might my need no not of on or our please so some than that the their them
then there these they this those to too um uh up us want was we well were what when where
which who why will with would yes you your yours im ive id youre dont cant hello okay ok
thanks thank tell know whats hows wheres thats theres isnt doesnt havent hasnt wont didnt arent
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_WORD = re.compile(r"[^\W_]+")


def fold(text: str) -> str:
    """Lowercase and strip accents so 'Médico' and 'medico' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    """Content words: folded, apostrophes dropped (don't -> dont), stopwords removed."""
    return [w for w in _WORD.findall(fold(text).replace("'", "")) if w not in STOPWORDS]


def stem(word: str) -> str:
    """A deliberately light suffix stripper (plurals, -ing, -ed, -ation, trailing e)."""
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ation", "ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    if len(word) > 4 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]  # cancell -> cancel, refill -> refil
    return word


def terms(text: str) -> list[str]:
    return [stem(w) for w in words(text)]


class BM25Ranker:
    """Okapi BM25 over stemmed content words, with optional typo-tolerant query expansion."""

    def __init__(
        self,
        documents: Sequence[str],
        *,
        k1: float = 1.2,
        b: float = 0.75,
        fuzzy_min_similarity: float | None = 0.6,
    ) -> None:
        self.name = "bm25" if fuzzy_min_similarity is not None else "bm25_exact"
        self._k1, self._b = k1, b
        self._fuzzy_min_similarity = fuzzy_min_similarity
        self._tfs = [Counter(terms(doc)) for doc in documents]
        self._lengths = [sum(tf.values()) for tf in self._tfs]
        self._avg_length = sum(self._lengths) / max(len(self._lengths), 1)
        n = len(documents)
        df = Counter(term for tf in self._tfs for term in tf)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._postings: dict[str, list[int]] = {}
        for index, tf in enumerate(self._tfs):
            for term in tf:
                self._postings.setdefault(term, []).append(index)
        self._term_grams = {term: _trigrams(term) for term in self._idf}

    def rank(self, query: str, top_n: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for term, weight in self.query_terms(query).items():
            idf = self._idf[term]
            for index in self._postings[term]:
                tf = self._tfs[index][term]
                norm = 1 - self._b + self._b * self._lengths[index] / self._avg_length
                scores[index] = scores.get(index, 0.0) + weight * idf * tf * (self._k1 + 1) / (
                    tf + self._k1 * norm
                )
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_n]

    def query_terms(self, query: str) -> dict[str, float]:
        """Indexed query terms with weights, tolerant of how speech recognition mangles words.

        - Adjacent words are also tried joined: ASR splits "telehealth" into "tele health".
        - A word that is not in the index matches its closest indexed term (trigram Dice
          >= fuzzy_min_similarity), weighted by that similarity: "perscription".
        """
        query_terms = terms(query)
        weights: dict[str, float] = {}
        for a, b in zip(query_terms, query_terms[1:], strict=False):
            if (joined := stem(a + b)) in self._idf:
                weights[joined] = 1.0
        for term in query_terms:
            if term in self._idf:
                weights[term] = 1.0
            elif self._fuzzy_min_similarity is not None:
                grams = _trigrams(term)
                similarity, closest = max(
                    ((_dice(grams, g), t) for t, g in self._term_grams.items()), default=(0.0, "")
                )
                if similarity >= self._fuzzy_min_similarity:
                    weights[closest] = max(weights.get(closest, 0.0), similarity)
        return weights


class CharNgramRanker:
    """TF-IDF cosine similarity over character n-grams taken within word boundaries."""

    name = "char_ngram"

    def __init__(self, documents: Sequence[str], *, n_min: int = 3, n_max: int = 5) -> None:
        self._n_min, self._n_max = n_min, n_max
        counts = [self._ngrams(doc) for doc in documents]
        n = len(documents)
        df = Counter(gram for c in counts for gram in c)
        self._idf = {g: math.log((1 + n) / (1 + d)) + 1 for g, d in df.items()}
        self._unseen_idf = math.log(1 + n) + 1  # what a gram in no document would get
        self._vectors = [self._weigh(c) for c in counts]
        self._postings: dict[str, list[int]] = {}
        for index, vector in enumerate(self._vectors):
            for gram in vector:
                self._postings.setdefault(gram, []).append(index)

    def rank(self, query: str, top_n: int) -> list[tuple[int, float]]:
        query_vector = self._weigh(self._ngrams(query))
        scores: dict[int, float] = {}
        for gram, weight in query_vector.items():
            for index in self._postings.get(gram, ()):
                scores[index] = scores.get(index, 0.0) + weight * self._vectors[index][gram]
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_n]

    def _ngrams(self, text: str) -> Counter[str]:
        grams: Counter[str] = Counter()
        for word in words(text):
            padded = f" {word} "
            for n in range(self._n_min, self._n_max + 1):
                grams.update(padded[i : i + n] for i in range(len(padded) - n + 1))
        return grams

    def _weigh(self, counts: Counter[str]) -> dict[str, float]:
        """Sublinear tf * idf, L2-normalised. Unknown n-grams still count toward the norm."""
        weights = {
            g: (1 + math.log(c)) * self._idf.get(g, self._unseen_idf) for g, c in counts.items()
        }
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {g: w / norm for g, w in weights.items() if g in self._idf}


class TermCoverage:
    """Confidence that a chunk answers a query: the share of the query's content words that
    appear in the chunk, matched approximately so speech-recognition misspellings still count.

    Each query word scores its best character-trigram Dice similarity to a chunk word (below
    `min_similarity` counts as absent). Words are weighted by length, capped at 6 letters: long
    words carry the meaning, short ones are often fragments of mis-heard function words.
    Unlike rank-fusion scores this is absolute, so it can reject out-of-scope questions.
    """

    def __init__(self, documents: Sequence[str], *, min_similarity: float = 0.5) -> None:
        self._min_similarity = min_similarity
        self._vocabularies = [{w: _trigrams(w) for w in set(words(doc))} for doc in documents]
        self._stems = [{stem(w) for w in vocabulary} for vocabulary in self._vocabularies]

    def __call__(self, query: str, index: int) -> float:
        query_words = words(query)
        if not query_words:
            return 0.0
        vocabulary = self._vocabularies[index]
        matched = total = 0.0
        for word in query_words:
            weight = min(len(word), 6)
            total += weight
            if stem(word) in self._stems[index]:
                matched += weight
                continue
            grams = _trigrams(word)
            best = max((_dice(grams, other) for other in vocabulary.values()), default=0.0)
            if best >= self._min_similarity:
                matched += weight * best
        return matched / total


def _trigrams(word: str) -> frozenset[str]:
    padded = f" {word} "
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def _dice(a: frozenset[str], b: frozenset[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b))
