"""Synthetic unit tests for ORBIT RAG scoring methods, obsolescence detection, and outdated tagging."""

from pathlib import Path

from config import settings
from rag.index import DocumentIndex, is_document_outdated
from rag.retrieval import auto_retrieve


def test_obsolescence_detection_true_and_false_cases():
    """Verify that obsolescence detection is true for notices and false for innocent mentions."""
    # Several synthetic notices that must evaluate to True:
    true_notices = [
        "This document is obsolete.",
        "This policy has been superseded by the 2026 guidelines.",
        "Please note that this version is no longer in effect.",
        "This standard was replaced by the updated specification.",
        "Superseded by internal guideline v3.",
        "Status: Outdated",
        "Status: superseded",
        "## Status: Deprecated",
        "> **STATUS: ARCHIVED POLICY - AUDIT ONLY**",
        "**Status:** archived",
        "**Status**: obsolete",
        "*Status:* deprecated",
        "Status: Superseded by new revision",
    ]
    for notice in true_notices:
        assert is_document_outdated(notice) is True, f"Expected True for notice: {notice!r}"

    # Ordinary sentences containing 'replaced' or 'archived' that must evaluate to False:
    false_sentences = [
        "The computer monitor was replaced by IT support yesterday.",
        "All server logs are archived to cold storage every month.",
        "We replaced the broken office chair.",
        "Status: Active. Records are archived periodically.",
        "Status: Approved. Previous drafts were replaced during review.",
        "This document explains how heating filters are replaced in the warehouse.",
        "The team archived the completed project repository.",
    ]
    for sentence in false_sentences:
        assert is_document_outdated(sentence) is False, f"Expected False for sentence: {sentence!r}"


def test_each_scoring_method_on_handmade_chunks(tmp_path: Path):
    """Verify that fraction, rarest3, and idf correctly score hand-made chunks."""
    db_path = tmp_path / "test_scoring.db"
    idx = DocumentIndex(path=db_path)

    # Hand-made chunks
    # Chunk 1: specific terms "jupiter", "telescope", "astronomy"
    # Chunk 2: common term "astronomy", general term "science"
    # Chunk 3: common term "astronomy", general term "physics"
    idx.upsert_document(
        "jupiter.md",
        "hash_jup",
        [("Jupiter observations with optical telescope in modern astronomy.", False)],
    )
    idx.upsert_document(
        "science.md",
        "hash_sci",
        [("General science and astronomy curriculum overview.", False)],
    )
    idx.upsert_document(
        "physics.md",
        "hash_phy",
        [("Introductory physics principles and observational astronomy.", False)],
    )

    assert idx.chunk_count() == 3
    df = idx.term_df(["jupiter", "telescope", "astronomy", "nonexistent"])
    assert df["jupiter"] == 1
    assert df["telescope"] == 1
    assert df["astronomy"] == 3
    assert df["nonexistent"] == 0

    query = "jupiter telescope astronomy"

    # Under fraction scoring: 3 query terms, chunk 1 matches 3 -> 3/3 = 1.0 >= 0.5
    res_frac = auto_retrieve(idx, query, scoring="fraction", min_coverage=0.5)
    assert res_frac.injected >= 1
    assert any(h["file"] == "jupiter.md" and h["kept"] for h in res_frac.hits)

    # Under rarest3 scoring:
    # Terms with df >= 1 are jupiter (1), telescope (1), astronomy (3).
    # Smallest 3 are jupiter, telescope, astronomy. Chunk 1 matches all 3 -> 3/3 = 1.0 >= 0.5
    res_rarest = auto_retrieve(idx, query, scoring="rarest3", min_coverage=0.5)
    assert res_rarest.injected >= 1
    assert any(h["file"] == "jupiter.md" and h["kept"] for h in res_rarest.hits)

    # Under idf scoring: Chunk 1 matches all active terms -> coverage = 1.0 >= 0.5
    res_idf = auto_retrieve(idx, query, scoring="idf", min_coverage=0.5)
    assert res_idf.injected >= 1
    assert any(h["file"] == "jupiter.md" and h["kept"] for h in res_idf.hits)


def test_chatty_question_fraction_fails_idf_succeeds(tmp_path: Path):
    """Verify a chatty question where fraction fails (< 0.5) and idf succeeds (>= 0.5)."""
    db_path = tmp_path / "test_chatty.db"
    idx = DocumentIndex(path=db_path)

    # 10 total chunks so N=10
    # Document with specific target info: "reimbursement" and "receipts" appear only here (df=1)
    idx.upsert_document(
        "expense.md",
        "hash_exp",
        [("Travel expense reimbursement requires original receipts submitted promptly.", False)],
    )
    # Common terms: "policy", "company", "team", "review" appear in 9 other documents (high df=9)
    for i in range(9):
        idx.upsert_document(
            f"doc_{i}.md",
            f"hash_{i}",
            [(f"Standard company policy review for team operations and procedure #{i}.", False)],
        )

    assert idx.chunk_count() == 10
    df = idx.term_df(["reimbursement", "receipts", "company", "policy", "team", "review"])
    assert df["reimbursement"] == 1
    assert df["receipts"] == 1
    assert df["policy"] == 9
    assert df["company"] == 9

    # Chatty question containing 2 rare terms ("reimbursement", "receipts")
    # and 4 common terms ("company", "policy", "team", "review")
    chatty_q = "Could you please tell me about reimbursement receipts under our company policy team review?"
    # Distinct terms: reimbursement, receipts, company, policy, team, review, tell, please
    # (6 or 7 terms after stopwords)

    # 1. Under fraction:
    # Target chunk expense.md matches only "reimbursement" and "receipts" (2 terms)
    # Total distinct terms >= 6 -> coverage = 2/6 = 0.333 < 0.5 -> DROPPED
    res_fraction = auto_retrieve(idx, chatty_q, scoring="fraction", min_coverage=0.5)
    exp_frac_hit = next((h for h in res_fraction.hits if h["file"] == "expense.md"), None)
    assert exp_frac_hit is not None
    assert exp_frac_hit["kept"] is False
    assert exp_frac_hit["matched_fraction"] < 0.5

    # 2. Under IDF:
    # Rare terms "reimbursement" (df=1) and "receipts" (df=1) have N/df = 10/1 = 10 -> w = ln(11) = 2.398
    # Common terms have N/df = 10/9 = 1.11 -> w = ln(2.11) = 0.747
    # Sum of matched rare terms carries majority weight -> coverage >= 0.5 -> KEPT
    res_idf = auto_retrieve(idx, chatty_q, scoring="idf", min_coverage=0.5)
    exp_idf_hit = next((h for h in res_idf.hits if h["file"] == "expense.md"), None)
    assert exp_idf_hit is not None
    assert exp_idf_hit["kept"] is True
    assert exp_idf_hit["matched_fraction"] >= 0.5


def test_general_question_two_common_corpus_words_gets_no_injection(tmp_path: Path):
    """Verify that a general question with two common corpus words gets NO injection under every method."""
    db_path = tmp_path / "test_common_no_inj.db"
    idx = DocumentIndex(path=db_path)

    # Create corpus where "system" appears in docs 1..4, and "network" appears in docs 5..8
    # No single chunk contains BOTH "system" and "network"
    for i in range(4):
        idx.upsert_document(
            f"sys_{i}.md",
            f"hash_sys_{i}",
            [(f"Computer operating system installation and maintenance manual #{i}.", False)],
        )
    for i in range(4):
        idx.upsert_document(
            f"net_{i}.md",
            f"hash_net_{i}",
            [(f"Local area network cabling and wireless switch manual #{i}.", False)],
        )

    # General question containing two common corpus words ("system", "network") and other general words
    gen_query = "What is the best way to design a decentralized system network architecture for weather monitoring?"

    for method in ("fraction", "rarest3", "idf"):
        res = auto_retrieve(idx, gen_query, scoring=method, min_coverage=0.5, min_terms=2)
        # Must NOT inject any chunk under any method because no chunk matches >= min_terms (2)
        assert res.injected == 0, f"Expected 0 injected chunks for method {method}, got {res.injected}"
        assert res.block == ""


def test_df_zero_terms_are_ignored(tmp_path: Path):
    """Verify that terms with df = 0 are ignored by rarest3 and idf without error or penalty."""
    db_path = tmp_path / "test_df_zero.db"
    idx = DocumentIndex(path=db_path)

    idx.upsert_document(
        "guide.md",
        "hash_g",
        [("Official handbook on employee benefits and health coverage.", False)],
    )

    # Query with 2 matching terms ("benefits", "coverage") and 2 terms with df=0 ("supercalifragilistic", "xylophone")
    q_with_zero = "benefits coverage supercalifragilistic xylophone"

    # 1. rarest3: terms with df >= 1 are benefits (1) and coverage (1).
    # Smallest df terms are [benefits, coverage]. Both match guide.md -> coverage = 2/2 = 1.0
    res_rarest = auto_retrieve(idx, q_with_zero, scoring="rarest3", min_coverage=0.5)
    hit_rarest = next(h for h in res_rarest.hits if h["file"] == "guide.md")
    assert hit_rarest["kept"] is True
    assert hit_rarest["matched_fraction"] == 1.0

    # 2. idf: terms with df=0 are ignored; total weight only sums terms with df >= 1
    # Both active terms match -> coverage = 1.0
    res_idf = auto_retrieve(idx, q_with_zero, scoring="idf", min_coverage=0.5)
    hit_idf = next(h for h in res_idf.hits if h["file"] == "guide.md")
    assert hit_idf["kept"] is True
    assert hit_idf["matched_fraction"] == 1.0


def test_outdated_passages_tagged_and_ordered_last(tmp_path: Path):
    """Verify that passages from outdated documents are tagged [OUTDATED...] and listed after current passages."""
    db_path = tmp_path / "test_outdated.db"
    idx = DocumentIndex(path=db_path)

    # Document 1: Outdated policy (detected via Status line)
    idx.upsert_document(
        "remote_work_policy_2022.md",
        "hash_2022",
        [(
            "Status: Superseded by the 2024 policy.\n"
            "Remote work stipends are strictly capped at $250 annually.",
            False,
        )],
    )

    # Document 2: Current active policy
    idx.upsert_document(
        "remote_work_policy_2024.md",
        "hash_2024",
        [(
            "Current company standard for remote work arrangements.\n"
            "Remote work stipends are authorized up to $1000 annually.",
            False,
        )],
    )

    # Verify database stored documents.outdated correctly
    with idx._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT path, outdated FROM documents ORDER BY path ASC")
        docs = cursor.fetchall()
        doc_map = {p: bool(o) for p, o in docs}
        assert doc_map["remote_work_policy_2022.md"] is True
        assert doc_map["remote_work_policy_2024.md"] is False

    query = "remote work stipends annually"
    res = auto_retrieve(idx, query, scoring="fraction", min_coverage=0.5)
    assert res.injected == 2

    # Verify hit records contain outdated flag
    h_2022 = next(h for h in res.hits if "2022" in h["file"])
    h_2024 = next(h for h in res.hits if "2024" in h["file"])
    assert h_2022["outdated"] is True
    assert h_2024["outdated"] is False

    # Verify block structure:
    # 1. Non-outdated (2024) passage appears before outdated (2022) passage
    pos_2024 = res.block.find("remote_work_policy_2024.md")
    pos_2022 = res.block.find("remote_work_policy_2022.md")
    assert pos_2024 != -1
    assert pos_2022 != -1
    assert pos_2024 < pos_2022, "Non-outdated passage must appear before outdated passage"

    # 2. Outdated passage has tag [OUTDATED: this document says it is superseded]
    assert "[OUTDATED: this document says it is superseded]" in res.block

    # 3. Header contains sentence warning the model about OUTDATED passages
    assert "Passages marked as OUTDATED may be wrong, and you must say so when relying on one." in res.block


def test_default_configuration_reproduces_baseline_numbers(tmp_path: Path):
    """Verify that default settings (scoring='fraction', coverage=0.5) reproduce exact baseline behavior."""
    db_path = tmp_path / "test_baseline.db"
    idx = DocumentIndex(path=db_path)

    idx.upsert_document(
        "handbook.md",
        "hash_hb",
        [(
            "Section 4: Vacation requests must be submitted at least two weeks in advance. "
            "Managers have three business days to approve or deny vacation requests.",
            False,
        )],
    )
    idx.upsert_document(
        "benefits.md",
        "hash_ben",
        [(
            "Section 8: Health insurance coverage begins on the first day of the month following hire date.",
            False,
        )],
    )

    # 1. Matching factual query
    q_match = "How many weeks in advance must vacation requests be submitted?"
    ret_match = auto_retrieve(idx, q_match)
    assert ret_match.searched is True
    assert ret_match.injected == 1
    assert ret_match.reason == "retrieved"
    assert "handbook.md#0" in ret_match.block

    # 2. General query that should not inject
    q_general = "What is the capital of France?"
    ret_general = auto_retrieve(idx, q_general)
    assert ret_general.searched is True
    assert ret_general.injected == 0
    assert ret_general.block == ""

    # 3. Personal unanswerable query that generates not-found note
    q_pna = "What is my manager's home phone number in the company directory?"
    ret_pna = auto_retrieve(idx, q_pna)
    assert ret_pna.searched is True
    assert ret_pna.injected == 0
    assert ret_pna.block == ""
    assert bool(ret_pna.note) is True


def test_default_rag_auto_scoring_and_min_coverage_are_preserved():
    """Verify that default RAG_AUTO_SCORING is 'fraction' and default min coverage is 0.5.

    Ensures that any silent default change to scoring method or coverage threshold is caught.
    """
    assert settings.RAG_AUTO_SCORING == "fraction", (
        f"Expected default RAG_AUTO_SCORING to be 'fraction', got {settings.RAG_AUTO_SCORING!r}"
    )
    assert settings.RAG_AUTO_MIN_COVERAGE == 0.5, (
        f"Expected default RAG_AUTO_MIN_COVERAGE to be 0.5, got {settings.RAG_AUTO_MIN_COVERAGE!r}"
    )

