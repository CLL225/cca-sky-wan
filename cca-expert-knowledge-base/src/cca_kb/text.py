"""Tokenization and search-document construction.

Keyword retrieval must work for both English source terms and Chinese
questions.  Latin text is tokenized on word boundaries; CJK runs are turned
into character bigrams.  A small bilingual lexicon bridges the most common
CCA terms so that Chinese questions can match English records even when the
vector backend is unavailable.  The lexicon is lexical, not a semantic model.
"""

from __future__ import annotations

import re
from typing import Iterable

from cca_kb.models import DesignExperience

LATIN_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
CJK_RE = re.compile(r"[㐀-䶿一-鿿]+")
SPLIT_RE = re.compile(r"[._\-/]")

STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could do does for from had has have
    how i if in into is it its may might more most no not of on or our over
    should so such than that the their them then there these they this those
    to under up use used using was we were what when which while who will with
    would
    """.split()
)

# Longest keys first so that 随机丢包 wins over 丢包.
BILINGUAL_LEXICON: dict[str, tuple[str, ...]] = {
    "随机丢包": ("random", "loss"),
    "拥塞丢包": ("congestion", "loss"),
    "丢包检测": ("loss", "detection"),
    "丢包响应": ("loss", "response"),
    "带宽估计": ("bandwidth", "estimation"),
    "带宽时延积": ("bandwidth", "delay", "product", "bdp"),
    "最小往返时延": ("min", "rtt"),
    "拥塞窗口": ("congestion", "window", "cwnd"),
    "发送速率": ("sending", "rate"),
    "发送节拍": ("pacing"),
    "拥塞控制": ("congestion", "control"),
    "缓冲区膨胀": ("bufferbloat"),
    "浅缓冲区": ("shallow", "buffer"),
    "缓冲区": ("buffer",),
    "数据中心": ("data", "center", "datacenter"),
    "高带宽时延积": ("high", "bdp"),
    "多流竞争": ("multi", "flow", "competition"),
    "状态机": ("state", "machine"),
    "公平性": ("fairness",),
    "吞吐量": ("throughput",),
    "稳定性": ("stability",),
    "收敛": ("convergence",),
    "鲁棒性": ("robustness",),
    "探测": ("probing",),
    "启动": ("startup",),
    "退出": ("exit",),
    "时延": ("delay", "latency"),
    "往返时延": ("rtt",),
    "丢包": ("loss",),
    "拥塞": ("congestion",),
    "带宽": ("bandwidth",),
    "队列": ("queue",),
    "无线": ("wireless",),
    "突发": ("burst",),
    "路径": ("path",),
    "增益": ("gain",),
    "阈值": ("threshold",),
    "参数": ("parameter",),
    "机制": ("mechanism",),
    "证据": ("evidence",),
}
_LEXICON_KEYS = sorted(BILINGUAL_LEXICON, key=len, reverse=True)


def tokenize(text: str) -> list[str]:
    """Return deterministic search tokens for a piece of text."""

    if not text:
        return []

    lower = text.lower()
    tokens: list[str] = []

    for match in LATIN_RE.finditer(lower):
        raw = match.group(0)
        parts = [part for part in SPLIT_RE.split(raw) if part]
        for part in parts:
            if len(part) >= 2 and part not in STOPWORDS:
                tokens.append(part)
        if raw not in parts and len(raw) >= 2 and raw not in STOPWORDS:
            tokens.append(raw)

    for match in CJK_RE.finditer(lower):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    # Chinese -> English lexical bridge.
    for key in _LEXICON_KEYS:
        if key in lower:
            tokens.extend(BILINGUAL_LEXICON[key])

    return tokens


def _flat(values: Iterable[object] | object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values] if values.strip() else []
    if isinstance(values, (list, tuple, set)):
        out: list[str] = []
        for value in values:
            out.extend(_flat(value))
        return out
    return [str(values)]


def experience_fields(exp: DesignExperience) -> dict[str, str]:
    """Flatten an experience into labelled text fields used by both indexes."""

    pc = exp.problem_context
    dr = exp.design_rationale
    dd = exp.design_decision
    mech = exp.mechanism
    tr = exp.tradeoffs
    app = exp.applicability
    ev = exp.evaluation
    params = exp.parameter_guidance

    return {
        "title": exp.title,
        "problem": " ".join(
            _flat(pc.network_conditions)
            + _flat(pc.observed_symptoms)
            + _flat(pc.underlying_cause)
        ),
        "rationale": " ".join(
            _flat(dr.motivation) + _flat(dr.problem_analysis) + _flat(dr.reasoning_chain)
        ),
        "decision": " ".join(
            _flat(dd.selected_mechanism)
            + _flat(dd.alternatives_considered)
            + _flat(dd.why_selected)
        ),
        "mechanism": " ".join(
            _flat(mech.description)
            + _flat(mech.control_signals)
            + _flat(mech.control_actions)
            + _flat(mech.state_variables)
        ),
        "tradeoffs": " ".join(
            _flat(tr.benefits) + _flat(tr.costs) + _flat(tr.limitations)
        ),
        "failure_modes": " ".join(_flat(exp.failure_modes)),
        "applicability": " ".join(
            _flat(app.suitable_conditions)
            + _flat(app.unsuitable_conditions)
            + _flat(app.assumptions)
        ),
        "parameters": " ".join(
            _flat([p.parameter for p in params])
            + _flat([p.guidance for p in params])
            + _flat([p.rationale for p in params])
        ),
        "evaluation": " ".join(
            _flat(ev.metrics) + _flat(ev.expected_effects) + _flat(ev.evaluation_conditions)
        ),
        "tags": " ".join(_flat(exp.metadata.tags)),
        "status": exp.knowledge_status,
        "type": exp.experience_type,
    }


def experience_search_text(exp: DesignExperience) -> str:
    fields = experience_fields(exp)
    return "\n".join(f"{name}: {value}" for name, value in fields.items() if value)


def experience_embedding_text(exp: DesignExperience) -> str:
    """Weighted text for the vector backend (title/problem/motivation matter most)."""

    fields = experience_fields(exp)
    weights = {
        "title": 3,
        "problem": 2,
        "rationale": 2,
        "decision": 2,
        "mechanism": 2,
        "tradeoffs": 2,
        "failure_modes": 1,
        "applicability": 2,
        "parameters": 1,
        "evaluation": 1,
        "tags": 2,
    }
    parts: list[str] = []
    for name, weight in weights.items():
        value = fields.get(name, "")
        if value:
            parts.extend([value] * weight)
    return "\n".join(parts)


def title_token_overlap(query_tokens: list[str], title: str) -> float:
    query = set(query_tokens)
    if not query:
        return 0.0
    return len(query & set(tokenize(title))) / len(query)
