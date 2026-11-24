"""Knowledge-base retrieval must sharpen prompts, never break generation.

The retriever shells out to an external corpus, so its failure modes matter as
much as its output: a missing script, a crash or malformed JSON has to degrade
to "no section" rather than take down an expensive evolution run.
"""

from __future__ import annotations

import json
from pathlib import Path

from skydiscover.optimize.context_builder.knowledge_base import (
    KnowledgeBaseRetriever,
    build_query,
)

_METRICS = {
    "fairness_penalty": 46.5,
    "fairness_diagnosis": "low_jain_and_mixed_share_violation",
    "fairness_min_jain": 0.62,
    "fairness_max_share": 0.82,
    "fairness_min_share": 0.18,
    "queue_delay_ms": 80.0,
    "min_rtt_ms": 40.0,
    "rttvar_ms": 8.0,
    "throughput_kbps": 15000.0,
    "baseline_bbr_throughput_kbps": 15000.0,
    "convergence_penalty": 1.0,
}

_RESULT = {
    "query": "q",
    "limitations": ["lexical retrieval only"],
    "experiences": [
        {
            "experience_id": "exp-002",
            "title": "CUBIC",
            "experience_type": "mechanism_design",
            "knowledge_status": "cross_source_synthesis",
            "problem": "AIMD grows too slowly on high-BDP paths.",
            "design_decision": "Cubic window growth plus a Reno-friendly region.",
            "benefits": ["faster ramp-up", "keeps Reno compatibility"],
            "costs": ["more state"],
            "failure_modes": ["shallow buffers still overflow"],
            "applicability": ["high-BDP paths"],
            "evidence": [
                {
                    "source_id": "src-006",
                    "evidence_level": "A",
                    "locator": "3. CUBIC Congestion Control",
                }
            ],
        }
    ],
}


def test_query_targets_the_two_costliest_symptoms():
    query = build_query(_METRICS)
    assert "多流公平性" in query
    assert "排队时延" in query
    # Convergence barely hurts here, so it must not dilute the query.
    assert "收敛" not in query


def test_query_keeps_a_topical_anchor_when_nothing_is_wrong():
    query = build_query({"fairness_diagnosis": "good", "convergence_penalty": 0.1})
    assert query == "QUIC 拥塞控制 设计 改进"
    assert build_query(None) == query


def test_render_keeps_provenance_and_evidence():
    retriever = KnowledgeBaseRetriever({"enabled": True, "script": __file__})
    section = retriever.render(_RESULT)
    assert "EXPERT DESIGN EXPERIENCE" in section
    assert "exp-002" in section
    assert "cross_source_synthesis" in section
    assert "src-006" in section
    assert "level A" in section
    assert "lexical retrieval only" in section
    # One complete experience, no dangling truncation marker.
    assert "omitted for length" not in section


def test_render_truncates_at_an_experience_boundary():
    retriever = KnowledgeBaseRetriever({"enabled": True, "script": __file__, "max_chars": 900})
    section = retriever.render(_RESULT)
    assert len(section) < 1200
    assert section.endswith("omitted for length)")


def _fake_script(tmp_path, stdout: str, counter: str | None = None):
    body = f"print({stdout!r})\n"
    if counter:
        body += f"open({counter!r}, 'a').write('x')\n"
    path = tmp_path / "fake_kb.py"
    path.write_text(body)
    return str(path)


def test_retrieval_runs_once_per_query(tmp_path):
    counter = str(tmp_path / "calls.txt")
    script = _fake_script(tmp_path, json.dumps(_RESULT), counter)
    retriever = KnowledgeBaseRetriever({"enabled": True, "script": script})

    first = retriever.section(_METRICS)
    second = retriever.section(_METRICS)

    assert first and first == second
    assert Path(counter).read_text() == "x"


def test_missing_script_is_inactive():
    retriever = KnowledgeBaseRetriever(
        {"enabled": True, "script": "does/not/exist.py"}, config_dir="/tmp"
    )
    assert not retriever.active
    assert retriever.section(_METRICS) == ""


def test_malformed_output_degrades_to_empty(tmp_path):
    script = _fake_script(tmp_path, "not json")
    retriever = KnowledgeBaseRetriever({"enabled": True, "script": script})
    assert retriever.section(_METRICS) == ""


def test_failed_script_degrades_to_empty(tmp_path):
    path = tmp_path / "boom.py"
    path.write_text("raise SystemExit(3)\n")
    retriever = KnowledgeBaseRetriever({"enabled": True, "script": str(path)})
    assert retriever.section(_METRICS) == ""


def test_disabled_without_settings():
    retriever = KnowledgeBaseRetriever(None)
    assert not retriever.active
    assert retriever.section(_METRICS) == ""
