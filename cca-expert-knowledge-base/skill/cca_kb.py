#!/usr/bin/env python3
"""Portable TCP/QUIC congestion-control design-experience knowledge base.

This file is intentionally dependency-free: it uses only the Python standard
library and the generated ``knowledge.json`` next to it.  Copy the ``skill/``
directory anywhere, or clone the repository and call it directly:

    python3 skill/cca_kb.py search "random loss on a high-BDP path" --top-k 3
    python3 skill/cca_kb.py get exp-001
    python3 skill/cca_kb.py related exp-001
    python3 skill/cca_kb.py source src-004
    python3 skill/cca_kb.py catalog

It can also be imported:

    import sys; sys.path.insert(0, "skill")
    from cca_kb import search_experiences, get_experience

The public function names mirror the full package so an LLM framework can
switch between the portable skill and the optional semantic backend without
changing its prompts.

Retrieval is lexical (BM25 + a small Chinese/English term bridge) plus
metadata filters.  It is not a dense semantic model; every response says so
in ``retrieval.semantic``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_DATA = Path(__file__).with_name("knowledge.json")

LATIN_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
CJK_RE = re.compile(r"[㐀-䶿一-鿿]+")
SPLIT_RE = re.compile(r"[._\-/]")

EVIDENCE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}
ALLOWED_FILTERS = {
    "experience_type",
    "knowledge_status",
    "tags",
    "all_tags",
    "source_id",
    "min_evidence_level",
    "network_condition",
    "has_failure_modes",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BM25:
    """Small pure-Python BM25 implementation."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n_docs = len(documents)
        self.freqs = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.avgdl = sum(self.lengths) / self.n_docs if self.n_docs else 0.0
        document_freq: Counter[str] = Counter()
        for doc in documents:
            document_freq.update(set(doc))
        self.idf = {
            term: math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))
            for term, df in document_freq.items()
        }

    def normalized(self, query_tokens: list[str]) -> list[float]:
        scores = [0.0] * self.n_docs
        if not self.n_docs or self.avgdl <= 0:
            return scores

        for index, freqs in enumerate(self.freqs):
            length = self.lengths[index]
            total = 0.0
            for term in dict.fromkeys(query_tokens):
                tf = freqs.get(term, 0)
                if not tf:
                    continue
                denominator = tf + self.k1 * (
                    1.0 - self.b + self.b * length / self.avgdl
                )
                total += self.idf.get(term, 0.0) * (
                    tf * (self.k1 + 1.0) / denominator
                )
            scores[index] = total

        maximum = max(scores) if scores else 0.0
        if maximum <= 0:
            return [0.0] * self.n_docs
        return [score / maximum for score in scores]


class KnowledgeBase:
    """Loads ``knowledge.json`` and exposes search / get / related APIs."""

    def __init__(self, data_path: str | Path | None = None):
        self.data_path = Path(data_path) if data_path else DEFAULT_DATA
        if not self.data_path.is_file():
            raise FileNotFoundError(
                f"knowledge bundle not found: {self.data_path}; "
                "run tools/export_skill.py after editing knowledge/"
            )
        with self.data_path.open("r", encoding="utf-8") as handle:
            self.data = json.load(handle)

        self.experiences: dict[str, dict] = {
            item["id"]: item for item in self.data.get("experiences", [])
        }
        self.sources: dict[str, dict] = {
            item["source_id"]: item for item in self.data.get("sources", [])
        }
        self.relations: list[dict] = self.data.get("relations", [])
        self.lexicon: dict[str, list[str]] = self.data.get("bilingual_lexicon", {})
        self.stopwords = frozenset(self.data.get("stopwords", []))
        self._lexicon_keys = sorted(self.lexicon, key=len, reverse=True)

        self._ids = sorted(self.experiences)
        self._documents = [
            self.tokenize(self.experiences[experience_id].get("search_text", ""))
            for experience_id in self._ids
        ]
        self._bm25 = BM25(self._documents)

    # -------------------------------------------------------------- tokenizer
    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        lower = text.lower()
        tokens: list[str] = []

        for match in LATIN_RE.finditer(lower):
            raw = match.group(0)
            parts = [part for part in SPLIT_RE.split(raw) if part]
            for part in parts:
                if len(part) >= 2 and part not in self.stopwords:
                    tokens.append(part)
            if raw not in parts and len(raw) >= 2 and raw not in self.stopwords:
                tokens.append(raw)

        for match in CJK_RE.finditer(lower):
            run = match.group(0)
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

        for key in self._lexicon_keys:
            if key in lower:
                tokens.extend(self.lexicon[key])

        return tokens

    # --------------------------------------------------------------- payloads
    def _source_brief(self, source_id: str) -> dict:
        source = self.sources.get(source_id)
        if source is None:
            return {"source_id": source_id, "missing": True}
        return {
            "source_id": source["source_id"],
            "title": source.get("title", ""),
            "source_type": source.get("source_type", ""),
            "authors": source.get("authors", []),
            "year": source.get("year"),
            "url": source.get("url", ""),
            "doi": source.get("doi", ""),
            "locators": source.get("locators", []),
        }

    def _evidence(self, exp: dict) -> list[dict]:
        payload = []
        for item in exp.get("evidence", []):
            enriched = dict(item)
            enriched["source"] = self._source_brief(item["source_id"])
            payload.append(enriched)
        return payload

    def _related(self, exp: dict) -> list[dict]:
        payload: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for relation in self.relations:
            if relation["from_id"] == exp["id"]:
                other_id, direction = relation["to_id"], "outgoing"
            elif relation["to_id"] == exp["id"]:
                other_id, direction = relation["from_id"], "incoming"
            else:
                continue
            key = (other_id, relation["relation"])
            if key in seen:
                continue
            seen.add(key)
            payload.append(
                {
                    "experience_id": other_id,
                    "title": self.experiences.get(other_id, {}).get("title", ""),
                    "relation": relation["relation"],
                    "direction": direction,
                    "note": relation.get("note", ""),
                    "knowledge_status": relation.get("knowledge_status", ""),
                }
            )

        linked = {item["experience_id"] for item in payload}
        for other_id in exp.get("related_experiences", []):
            if other_id in linked:
                continue
            payload.append(
                {
                    "experience_id": other_id,
                    "title": self.experiences.get(other_id, {}).get("title", ""),
                    "relation": "related_to",
                    "direction": "unspecified",
                    "note": "",
                    "knowledge_status": "source_reported",
                }
            )
        return payload

    def _full_experience(self, exp: dict) -> dict:
        payload = dict(exp)
        payload.pop("search_text", None)
        payload.pop("summary", None)
        payload["experience_id"] = payload.pop("id")
        payload["evidence"] = self._evidence(exp)
        payload["related_experiences"] = self._related(exp)
        payload["sources"] = {
            item["source_id"]: self._source_brief(item["source_id"])
            for item in exp.get("evidence", [])
        }
        return payload

    def _search_item(
        self, exp: dict, score: float, matched_terms: list[str]
    ) -> dict:
        pc = exp.get("problem_context", {})
        dd = exp.get("design_decision", {})
        tr = exp.get("tradeoffs", {})
        app = exp.get("applicability", {})
        problem = pc.get("underlying_cause") or "；".join(
            pc.get("observed_symptoms", [])
        )
        return {
            "experience_id": exp["id"],
            "title": exp.get("title", ""),
            "experience_type": exp.get("experience_type", ""),
            "knowledge_status": exp.get("knowledge_status", ""),
            "tags": exp.get("metadata", {}).get("tags", []),
            "score": round(score, 4),
            "matched_terms": matched_terms[:12],
            "problem": _truncate(problem, 240),
            "design_decision": _truncate(
                dd.get("selected_mechanism", ""), 240
            ),
            "benefits": tr.get("benefits", [])[:3],
            "costs": tr.get("costs", [])[:3],
            "failure_modes": exp.get("failure_modes", [])[:3],
            "applicability": app.get("suitable_conditions", [])[:3],
            "evidence": [
                {
                    "source_id": item["source_id"],
                    "source_title": self.sources.get(item["source_id"], {}).get(
                        "title", ""
                    ),
                    "locator": item.get("locator", ""),
                    "evidence_level": item.get("evidence_level", ""),
                    "supports": item.get("supports", []),
                }
                for item in exp.get("evidence", [])
            ],
        }

    # --------------------------------------------------------------- filters
    def _matches(self, exp: dict, filters: dict) -> bool:
        if "experience_type" in filters:
            if exp.get("experience_type") not in _as_list(
                filters["experience_type"]
            ):
                return False
        if "knowledge_status" in filters:
            if exp.get("knowledge_status") not in _as_list(
                filters["knowledge_status"]
            ):
                return False
        if "tags" in filters:
            wanted = set(_as_list(filters["tags"]))
            if wanted and not wanted & set(exp.get("metadata", {}).get("tags", [])):
                return False
        if "all_tags" in filters:
            wanted = set(_as_list(filters["all_tags"]))
            tags = set(exp.get("metadata", {}).get("tags", []))
            if wanted and not wanted.issubset(tags):
                return False
        if "source_id" in filters:
            wanted = set(_as_list(filters["source_id"]))
            evidence_sources = {
                item["source_id"] for item in exp.get("evidence", [])
            }
            if wanted and not wanted & evidence_sources:
                return False
        if "min_evidence_level" in filters:
            level = str(filters["min_evidence_level"]).upper()
            if level not in EVIDENCE_RANK:
                raise ValueError("min_evidence_level must be one of A/B/C/D")
            best = min(
                EVIDENCE_RANK[item["evidence_level"]]
                for item in exp.get("evidence", [])
            )
            if best > EVIDENCE_RANK[level]:
                return False
        if "network_condition" in filters:
            pc = exp.get("problem_context", {})
            haystack = " ".join(
                pc.get("network_conditions", [])
                + pc.get("observed_symptoms", [])
                + [pc.get("underlying_cause", "")]
            )
            query_tokens = set(self.tokenize(str(filters["network_condition"])))
            if query_tokens:
                overlap = len(query_tokens & set(self.tokenize(haystack)))
                if overlap / len(query_tokens) < 0.5:
                    return False
        if "has_failure_modes" in filters:
            if bool(exp.get("failure_modes")) != bool(
                filters["has_failure_modes"]
            ):
                return False
        return True

    # ------------------------------------------------------------------- API
    def search_experiences(
        self,
        query: str,
        filters: dict | None = None,
        top_k: int = 5,
    ) -> dict:
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        filters = dict(filters or {})
        unknown = set(filters) - ALLOWED_FILTERS
        if unknown:
            raise ValueError(
                "unknown filter(s): "
                + ", ".join(sorted(unknown))
                + "; allowed: "
                + ", ".join(sorted(ALLOWED_FILTERS))
            )

        query_tokens = self.tokenize(query)
        keyword_scores = self._bm25.normalized(query_tokens)

        hits: list[tuple[float, dict, list[str]]] = []
        for index, experience_id in enumerate(self._ids):
            exp = self.experiences[experience_id]
            if not self._matches(exp, filters):
                continue
            title_tokens = set(self.tokenize(exp.get("title", "")))
            title_overlap = (
                len(set(query_tokens) & title_tokens) / len(set(query_tokens))
                if query_tokens
                else 0.0
            )
            score = 0.95 * keyword_scores[index] + 0.05 * title_overlap
            if score <= 0:
                continue
            matched = sorted(set(query_tokens) & set(self._documents[index]))
            hits.append((score, exp, matched))

        hits.sort(key=lambda item: (-item[0], item[1]["id"]))
        hits = hits[:top_k]

        return {
            "query": query,
            "filters": filters,
            "top_k": top_k,
            "retrieval": {
                "backend": "bm25-lexical-stdlib",
                "semantic": False,
                "returned": len(hits),
            },
            "experiences": [
                self._search_item(exp, score, matched)
                for score, exp, matched in hits
            ],
            "limitations": self._limitations(hits, filters),
        }

    def get_experience(self, experience_id: str) -> dict:
        exp = self._experience(experience_id)
        payload = self._full_experience(exp)
        payload["evidence_summary"] = [
            {
                "source_id": item["source_id"],
                "source_title": self.sources.get(item["source_id"], {}).get(
                    "title", ""
                ),
                "locator": item.get("locator", ""),
                "evidence_level": item.get("evidence_level", ""),
                "supports": item.get("supports", []),
            }
            for item in exp.get("evidence", [])
        ]
        return payload

    def get_source(self, source_id: str) -> dict:
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(f"unknown source id: {source_id}")
        payload = dict(source)
        payload["used_by"] = [
            {
                "experience_id": exp["id"],
                "title": exp.get("title", ""),
                "locator": evidence.get("locator", ""),
                "evidence_level": evidence.get("evidence_level", ""),
                "supports": evidence.get("supports", []),
            }
            for exp in self.experiences.values()
            for evidence in exp.get("evidence", [])
            if evidence["source_id"] == source_id
        ]
        return payload

    def get_related_experiences(self, experience_id: str) -> dict:
        exp = self._experience(experience_id)
        return {
            "experience_id": exp["id"],
            "title": exp.get("title", ""),
            "related_experiences": self._related(exp),
        }

    def catalog(self) -> dict:
        return {
            "count": len(self._ids),
            "experiences": [
                {
                    "experience_id": exp["id"],
                    "title": exp.get("title", ""),
                    "experience_type": exp.get("experience_type", ""),
                    "knowledge_status": exp.get("knowledge_status", ""),
                    "tags": exp.get("metadata", {}).get("tags", []),
                    "summary": exp.get("summary", ""),
                }
                for exp in (self.experiences[i] for i in self._ids)
            ],
        }

    def stats(self) -> dict:
        statuses: Counter[str] = Counter()
        types: Counter[str] = Counter()
        for exp in self.experiences.values():
            statuses[exp.get("knowledge_status", "")] += 1
            types[exp.get("experience_type", "")] += 1
        return {
            "experiences": len(self.experiences),
            "sources": len(self.sources),
            "relations": len(self.relations),
            "by_knowledge_status": dict(sorted(statuses.items())),
            "by_experience_type": dict(sorted(types.items())),
        }

    # ------------------------------------------------------------- internals
    def _experience(self, experience_id: str) -> dict:
        try:
            return self.experiences[experience_id]
        except KeyError as exc:
            raise KeyError(f"unknown experience id: {experience_id}") from exc

    def _limitations(
        self, hits: list[tuple[float, dict, list[str]]], filters: dict
    ) -> list[str]:
        if not hits:
            return [
                "没有找到匹配的经验；尝试更具体的拥塞控制术语，"
                "或用 catalog 浏览全部条目。"
            ]

        limitations: list[str] = []
        inference_ids = [
            exp["id"]
            for _, exp, _ in hits
            if exp.get("knowledge_status") == "inference"
        ]
        if inference_ids:
            limitations.append(
                "结果包含知识库推论（inference）："
                + "、".join(inference_ids)
                + "。这不是原作者明确结论，使用前应核对证据并标注为推论。"
            )

        weak_ids = []
        for _, exp, _ in hits:
            levels = [item["evidence_level"] for item in exp.get("evidence", [])]
            if levels and min(levels, key=lambda level: EVIDENCE_RANK[level]) in {
                "C",
                "D",
            }:
                weak_ids.append(exp["id"])
        if weak_ids:
            limitations.append(
                "结果包含仅有 C/D 级证据的经验："
                + "、".join(weak_ids)
                + "。C 表示有限或间接支持，D 表示缺少直接验证。"
            )

        if hits[0][0] < 0.25:
            limitations.append(
                f"最高相关度得分较低（{hits[0][0]:.2f}）；"
                "结果可能只是主题相近，建议补充网络条件或换用领域术语。"
            )
        if filters:
            limitations.append(
                "已应用元数据过滤；未返回的经验不代表知识库中没有相关记录。"
            )
        return limitations


# --------------------------------------------------------------- module API
_DEFAULT_BASE: KnowledgeBase | None = None


def _base(data_path: str | Path | None = None) -> KnowledgeBase:
    global _DEFAULT_BASE
    if data_path is None:
        if _DEFAULT_BASE is None:
            _DEFAULT_BASE = KnowledgeBase()
        return _DEFAULT_BASE
    return KnowledgeBase(data_path)


def search_experiences(
    query: str,
    filters: dict | None = None,
    top_k: int = 5,
    *,
    data_path: str | Path | None = None,
) -> dict:
    return _base(data_path).search_experiences(query, filters, top_k)


def get_experience(
    experience_id: str, *, data_path: str | Path | None = None
) -> dict:
    return _base(data_path).get_experience(experience_id)


def get_source(source_id: str, *, data_path: str | Path | None = None) -> dict:
    return _base(data_path).get_source(source_id)


def get_related_experiences(
    experience_id: str, *, data_path: str | Path | None = None
) -> dict:
    return _base(data_path).get_related_experiences(experience_id)


def catalog(*, data_path: str | Path | None = None) -> dict:
    return _base(data_path).catalog()


# ----------------------------------------------------------------------- CLI
def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_search(result: dict) -> None:
    print(f"query: {result['query']}")
    print(
        f"backend: {result['retrieval']['backend']} "
        f"(semantic={result['retrieval']['semantic']}, "
        f"returned={result['retrieval']['returned']})"
    )
    print()
    if not result["experiences"]:
        print("(no matching experiences)")
    for rank, exp in enumerate(result["experiences"], start=1):
        print(
            f"{rank}. [{exp['experience_id']}] {exp['title']}  "
            f"score={exp['score']:.3f}"
        )
        print(
            f"   type={exp['experience_type']}  "
            f"status={exp['knowledge_status']}  "
            f"tags={', '.join(exp['tags']) or '-'}"
        )
        if exp["problem"]:
            print(f"   problem: {exp['problem']}")
        if exp["design_decision"]:
            print(f"   decision: {exp['design_decision']}")
        for evidence in exp["evidence"]:
            print(
                f"   evidence: {evidence['evidence_level']} | "
                f"{evidence['source_title']} | {evidence['locator']}"
            )
        print()
    if result["limitations"]:
        print("limitations:")
        for limitation in result["limitations"]:
            print(f"  - {limitation}")


def _print_get(payload: dict) -> None:
    print(f"[{payload['experience_id']}] {payload['title']}")
    print(f"type: {payload['experience_type']}")
    print(f"status: {payload['knowledge_status']}")
    print(f"tags: {', '.join(payload['metadata'].get('tags', []))}")
    pc = payload["problem_context"]
    print("\nproblem:")
    print(
        f"  {pc.get('underlying_cause') or '；'.join(pc.get('observed_symptoms', []))}"
    )
    print("\nmotivation:")
    print(f"  {payload['design_rationale'].get('motivation', '')}")
    print("\ndecision:")
    print(f"  {payload['design_decision'].get('selected_mechanism', '')}")
    print("\nmechanism:")
    print(f"  {payload['mechanism'].get('description', '')}")
    print("\ntradeoffs:")
    for label in ("benefits", "costs", "limitations"):
        values = payload["tradeoffs"].get(label, [])
        if values:
            print(f"  {label}: " + "；".join(values))
    if payload.get("failure_modes"):
        print("\nfailure modes:")
        for item in payload["failure_modes"]:
            print(f"  - {item}")
    print("\nevidence:")
    for evidence in payload["evidence"]:
        print(
            f"  - {evidence['evidence_level']} {evidence['source_id']} "
            f"({evidence['source'].get('title', '')}) @ {evidence['locator']}"
        )
    print("\nrelated:")
    for related in payload["related_experiences"]:
        print(
            f"  - {related['experience_id']} [{related['relation']}] "
            f"{related['title']}"
        )


def _print_source(payload: dict) -> None:
    print(f"[{payload['source_id']}] {payload['title']}")
    print(f"type: {payload['source_type']}  year: {payload.get('year')}")
    print(f"authors: {', '.join(payload.get('authors', []))}")
    print(f"venue: {payload.get('venue', '')}")
    print(f"url: {payload.get('url', '')}")
    if payload.get("doi"):
        print(f"doi: {payload['doi']}")
    print("\nlocators:")
    for locator in payload.get("locators", []):
        print(f"  - {locator['type']}: {locator['value']}")
    print("\nused by:")
    for usage in payload["used_by"]:
        print(
            f"  - {usage['experience_id']} @ {usage['locator']} "
            f"({usage['evidence_level']})"
        )


def _print_related(payload: dict) -> None:
    print(f"[{payload['experience_id']}] {payload['title']}")
    for related in payload["related_experiences"]:
        print(
            f"  - {related['experience_id']} [{related['relation']}] "
            f"{related['title']}"
        )


def _print_catalog(payload: dict) -> None:
    print(f"{payload['count']} experiences")
    for exp in payload["experiences"]:
        print(
            f"[{exp['experience_id']}] {exp['title']} "
            f"({exp['knowledge_status']})"
        )
        if exp["summary"]:
            print(f"  {_truncate(exp['summary'], 200)}")


def _filters_from_args(args: argparse.Namespace) -> dict:
    filters: dict[str, Any] = {}
    if getattr(args, "experience_type", None):
        filters["experience_type"] = args.experience_type
    if getattr(args, "knowledge_status", None):
        filters["knowledge_status"] = args.knowledge_status
    if getattr(args, "tag", None):
        filters["tags"] = args.tag
    if getattr(args, "all_tag", None):
        filters["all_tags"] = args.all_tag
    if getattr(args, "source", None):
        filters["source_id"] = args.source
    if getattr(args, "min_evidence", None):
        filters["min_evidence_level"] = args.min_evidence
    if getattr(args, "network_condition", None):
        filters["network_condition"] = args.network_condition
    if getattr(args, "has_failure_modes", None) is not None:
        filters["has_failure_modes"] = args.has_failure_modes
    return filters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cca-kb-skill",
        description=(
            "Zero-dependency TCP/QUIC congestion-control design-experience "
            "knowledge base"
        ),
    )
    parser.add_argument(
        "--data",
        help="path to knowledge.json (defaults to the file next to this script)",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="human-readable output instead of JSON (may also follow a subcommand)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="search design experiences")
    search.add_argument("query", nargs="+")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--type", dest="experience_type")
    search.add_argument("--status", dest="knowledge_status")
    search.add_argument("--tag", action="append")
    search.add_argument("--all-tag", action="append")
    search.add_argument("--source", action="append")
    search.add_argument("--min-evidence", choices=["A", "B", "C", "D"])
    search.add_argument("--network-condition")
    search.add_argument("--has-failure-modes", action=argparse.BooleanOptionalAction)
    search.set_defaults(func="search")

    get = subparsers.add_parser("get", help="fetch one experience")
    get.add_argument("experience_id")
    get.set_defaults(func="get")

    source = subparsers.add_parser("source", help="fetch one source record")
    source.add_argument("source_id")
    source.set_defaults(func="source")

    related = subparsers.add_parser("related", help="follow typed relations")
    related.add_argument("experience_id")
    related.set_defaults(func="related")

    catalog_parser = subparsers.add_parser("catalog", help="list all experiences")
    catalog_parser.set_defaults(func="catalog")

    stats = subparsers.add_parser("stats", help="show counts")
    stats.set_defaults(func="stats")

    # Allow `--text` after the subcommand as well as before it.
    for subparser in (search, get, source, related, catalog_parser, stats):
        subparser.add_argument(
            "--text", action="store_true", default=argparse.SUPPRESS
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    text = bool(getattr(args, "text", False))
    try:
        base = _base(args.data)
        if args.func == "search":
            query = " ".join(args.query)
            payload = base.search_experiences(
                query, filters=_filters_from_args(args), top_k=args.top_k
            )
            _print_search(payload) if text else _print_json(payload)
        elif args.func == "get":
            payload = base.get_experience(args.experience_id)
            _print_get(payload) if text else _print_json(payload)
        elif args.func == "source":
            payload = base.get_source(args.source_id)
            _print_source(payload) if text else _print_json(payload)
        elif args.func == "related":
            payload = base.get_related_experiences(args.experience_id)
            _print_related(payload) if text else _print_json(payload)
        elif args.func == "catalog":
            payload = base.catalog()
            _print_catalog(payload) if text else _print_json(payload)
        elif args.func == "stats":
            _print_json(base.stats())
        else:  # pragma: no cover - argparse enforces the choices
            raise ValueError(f"unknown command: {args.func}")
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
