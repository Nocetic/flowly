"""The KG summary a prompt receives, and what a voice call may not see of it."""
from flowly.memory.knowledge_graph import KnowledgeGraph


def _graph(tmp_path):
    kg = KnowledgeGraph(str(tmp_path / "knowledge_graph.sqlite3"))
    works = kg.add_triple("Hakan", "works_at", "Nocetic", subject_type="person", object_type="company")
    email = kg.add_triple("Hakan", "email", "hakan@example.com", subject_type="person")
    secret = kg.add_triple("Ece", "diagnosed_with", "Condition", subject_type="person", object_type="topic")
    return kg, works, email, secret


def test_summary_without_exclusions_is_unchanged(tmp_path):
    kg, *_ = _graph(tmp_path)
    text = kg.summary(max_entities=20)
    assert "- Hakan (person): email=hakan@example.com, works_at → Nocetic" in text
    assert "Ece" in text


def test_excluded_triples_vanish_and_an_entity_left_empty_is_not_listed(tmp_path):
    kg, works, email, secret = _graph(tmp_path)
    text = kg.summary(max_entities=20, exclude_triple_ids={secret, email})
    assert "diagnosed_with" not in text and "Ece" not in text
    assert "hakan@example.com" not in text
    assert "- Hakan (person): works_at → Nocetic" in text
    # The exclusion is per call: the next summary sees everything again.
    assert "Ece" in kg.summary(max_entities=20)


def test_many_exclusions_do_not_hit_sqlite_parameter_limits(tmp_path):
    kg, works, *_ = _graph(tmp_path)
    many = {f"missing-{index}" for index in range(5_000)} | {works}
    assert "works_at" not in kg.summary(max_entities=20, exclude_triple_ids=many)
