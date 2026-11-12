import pytest

from receptionist.common import fixtures
from receptionist.mcp_server.db import BOOKING_HORIZON, LATE_CANCELLATION_FEE_USD
from receptionist.mcp_server.rag.chunking import (
    Document,
    chunk_document,
    load_documents,
    parse_document,
)
from receptionist.mcp_server.rag.embeddings import load_dense_ranker
from receptionist.mcp_server.rag.evaluate import evaluate, load_golden_set
from receptionist.mcp_server.rag.lexical import (
    BM25Ranker,
    CharNgramRanker,
    TermCoverage,
    stem,
)
from receptionist.mcp_server.rag.retriever import (
    Retriever,
    build_retriever,
    load_retriever,
    reciprocal_rank_fusion,
)
from receptionist.mcp_server.settings import DEFAULT_KB_DIR

LANGUAGE_NAMES = {"es": "Spanish", "hi": "Hindi", "ja": "Japanese", "de": "German", "fr": "French"}


@pytest.fixture(scope="module")
def retriever() -> Retriever:
    return load_retriever(DEFAULT_KB_DIR)


@pytest.fixture(scope="module")
def kb() -> dict[str, Document]:
    return {doc.doc_id: doc for doc in load_documents(DEFAULT_KB_DIR)}


# --------------------------------------------------------------------------- chunking


def test_front_matter_or_fallbacks_give_each_document_an_id_and_title() -> None:
    with_meta = parse_document(
        "---\ndoc_id: a\ntitle: A title\n---\n# Ignored\ntext", fallback_id="x"
    )
    without = parse_document("# Heading title\n\ntext", fallback_id="file-stem")
    assert (with_meta.doc_id, with_meta.title) == ("a", "A title")
    assert (without.doc_id, without.title) == ("file-stem", "Heading title")


def test_chunks_follow_headings_and_keep_metadata() -> None:
    doc = parse_document(
        "# Lab\n\n## Hours\n\n| Day | Time |\n|---|---|\n| Monday | 8 to 5 |\n\n"
        "## Fasting\n\n- Drink **water** only,\n  nothing else.\n- Take your meds.\n",
        fallback_id="lab",
    )
    (chunk,) = chunk_document(doc, min_words=0)[:1]
    assert (chunk.chunk_id, chunk.doc_id, chunk.title, chunk.section) == (
        "lab#0",
        "lab",
        "Lab",
        "Hours",
    )
    assert "Monday: 8 to 5" in chunk.text and "---" not in chunk.text
    fasting = chunk_document(doc, min_words=0)[1]
    assert fasting.text.splitlines() == ["- Drink water only, nothing else.", "- Take your meds."]


def test_run_on_text_is_still_bounded() -> None:
    doc = Document("d", "Doc", "## Wall\n\n" + "word " * 500)
    assert all(len(c.text.split()) <= 100 for c in chunk_document(doc, max_words=100))


def test_long_sections_are_split_with_overlap_and_short_ones_merged() -> None:
    paragraphs = "\n\n".join(f"Paragraph {n} " + "word " * 30 for n in range(10))
    doc = Document(
        "d", "Doc", f"## Long\n\n{paragraphs}\n\n## Tiny\n\nshort\n\n## Tinier\n\nshorter"
    )
    chunks = chunk_document(doc, max_words=100, min_words=20, overlap_words=40)
    long_chunks = [c for c in chunks if c.section == "Long"]
    assert len(long_chunks) > 1
    assert all(len(c.text.split()) <= 100 for c in chunks)
    for previous, current in zip(long_chunks, long_chunks[1:], strict=False):
        assert current.text.splitlines()[0] == previous.text.splitlines()[-1]  # one-block overlap
    assert chunks[-1].section == "Tiny / Tinier"


# --------------------------------------------------------------------------- rankers


def test_stemmer_conflates_common_variants() -> None:
    assert stem("refills") == stem("refill") == stem("refil")
    assert stem("cancelled") == stem("cancel") == stem("cancellation")
    assert stem("parking") == stem("park")


def test_bm25_prefers_rare_matching_terms() -> None:
    docs = ["parking garage on pier lane", "parking for bikes", "flu shots and vaccines"]
    ranker = BM25Ranker(docs)
    assert [i for i, _ in ranker.rank("where is the garage parking", 3)] == [0, 1]
    assert ranker.rank("weather", 3) == []


def test_bm25_tolerates_split_and_misspelled_words() -> None:
    ranker = BM25Ranker(["telehealth video visits", "prescription refills", "parking"])
    assert ranker.rank("tele health", 1)[0][0] == 0
    assert ranker.rank("perscription", 1)[0][0] == 1
    assert (
        BM25Ranker(["prescription refills"], fuzzy_min_similarity=None).rank("perscription", 1)
        == []
    )


def test_char_ngrams_survive_asr_misspellings() -> None:
    docs = ["prescription refills", "lab work and fasting", "parking garage"]
    assert CharNgramRanker(docs).rank("presciption refil", 1)[0][0] == 0


def test_term_coverage_is_an_absolute_relevance_signal() -> None:
    coverage = TermCoverage(["Seasonal flu shots are available for everyone"])
    assert coverage("when can I get a flu shot", 0) == 1.0
    assert coverage("when can I get a flue shott", 0) > 0.5
    assert coverage("who won the football game", 0) == 0.0


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = reciprocal_rank_fusion([[(1, 9.0), (2, 5.0)], [(2, 0.9), (3, 0.8)]], k=60)
    assert [i for i, _ in fused] == [2, 1, 3]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_dense_ranker_is_skipped_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_dense_ranker("none", ["text"]) is None
    assert load_dense_ranker("openai", ["text"]) is None
    retriever = build_retriever(DEFAULT_KB_DIR, dense_backend="openai")
    assert retriever.ranker_names == ["bm25", "char_ngram"]


# --------------------------------------------------------------------------- retrieval quality


@pytest.mark.parametrize(
    ("query", "doc_id"),
    [
        ("are you open on saturday", "hours-location"),
        ("do you take Northwind HMO", "insurance"),
        ("what's the fee for a late cancellation", "cancellation-policy"),
        ("presciption refil", "prescription-refills"),
        ("tele health visit", "telehealth"),
        ("can I pay with a credit card over the phone", "billing"),
        ("is there an after hours nurse", "urgent-emergency"),
    ],
)
def test_in_scope_questions_find_the_right_document(
    retriever: Retriever, query: str, doc_id: str
) -> None:
    result = retriever.search(query, top_k=3)
    assert result.confident
    assert doc_id in [hit.chunk.doc_id for hit in result.hits]


@pytest.mark.parametrize("query", ["what's the capital of france", "how do I cook brown rice"])
def test_out_of_scope_questions_get_no_confident_match(retriever: Retriever, query: str) -> None:
    assert not retriever.search(query).confident


def test_golden_set_quality_floor(retriever: Retriever) -> None:
    """Regression guard; the full report comes from `python -m ...rag.evaluate`."""
    cases = load_golden_set()
    assert len(cases) >= 25
    metrics = evaluate(retriever, cases)["metrics"]
    assert metrics["recall@3"] >= 0.9
    assert metrics["mrr"] >= 0.85
    assert metrics["out_of_scope_rejection_rate"] >= 0.8
    assert metrics["in_scope_false_rejection_rate"] <= 0.1


# --------------------------------------------------------------------------- KB consistency


def test_knowledge_base_size_and_ids(kb: dict[str, Document]) -> None:
    assert 14 <= len(kb) <= 18
    assert all(doc.title and doc.body.strip() for doc in kb.values())


def test_insurance_doc_matches_accepted_plans(kb: dict[str, Document]) -> None:
    for plan in fixtures.ACCEPTED_INSURANCE:
        assert plan in kb["insurance"].body


def test_provider_docs_match_the_roster(kb: dict[str, Document]) -> None:
    paragraphs = [" ".join(p.split()) for p in kb["providers"].body.split("\n\n")]
    languages = kb["interpreters-languages"].body
    for provider in fixtures.PROVIDERS:
        (about,) = [p for p in paragraphs if f"**{provider.name}**" in p]
        assert ("not accepting new patients" in about) != provider.accepts_new_patients
        for code in provider.languages:
            if code != "en":
                assert LANGUAGE_NAMES[code] in about
                assert f"{LANGUAGE_NAMES[code]}: {provider.name}" in languages


def test_policy_docs_match_the_booking_rules(kb: dict[str, Document]) -> None:
    policy = kb["cancellation-policy"].body
    assert f"${LATE_CANCELLATION_FEE_USD:.0f} late cancellation fee" in policy
    assert "less than 24 hours" in policy
    assert f"{BOOKING_HORIZON.days} days" in kb["appointments"].body
    hours = kb["hours-location"].body
    assert "Monday to Friday | 8:00 AM to 6:00 PM" in hours
    assert "Saturday | 9:00 AM to 1:00 PM" in hours
    assert fixtures.CLINIC["address"] in hours
