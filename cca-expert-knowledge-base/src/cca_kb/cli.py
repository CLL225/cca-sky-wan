"""Command-line interface.

All project operations (validate, quality, search, fetch, stats) are run
through this CLI inside the Docker container.  ``--json`` emits the same
structured payloads the Python API returns.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cca_kb.config import Settings
from cca_kb.quality import check_repository
from cca_kb.repository import KnowledgeError, KnowledgeRepository
from cca_kb.retrieval import RetrievalService


def _dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _truncate(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_search(result: dict[str, Any]) -> None:
    print(f"query: {result['query']}")
    backend = result["retrieval"]
    print(
        f"backend: {backend['backend']} "
        f"(semantic={backend['semantic']}, returned={backend['returned']})"
    )
    if result["filters"]:
        print(f"filters: {json.dumps(result['filters'], ensure_ascii=False)}")
    print()

    if not result["experiences"]:
        print("(no matching experiences)")
    for rank, exp in enumerate(result["experiences"], start=1):
        scores = exp["retrieval"]
        print(
            f"{rank}. [{exp['experience_id']}] {exp['title']}"
            f"  score={scores['score']:.3f}"
            f" (kw={scores['keyword_score']:.2f}, vec={scores['semantic_score']:.2f})"
        )
        print(
            f"   type={exp['experience_type']}  status={exp['knowledge_status']}  "
            f"tags={', '.join(exp['metadata']['tags']) or '-'}"
        )
        problem = exp["problem_context"].get("underlying_cause") or " ".join(
            exp["problem_context"].get("observed_symptoms", [])
        )
        if problem:
            print(f"   problem: {_truncate(problem)}")
        mechanism = exp["design_decision"].get("selected_mechanism") or exp[
            "mechanism"
        ].get("description", "")
        if mechanism:
            print(f"   mechanism: {_truncate(mechanism)}")
        for evidence in exp["evidence"]:
            source = evidence["source"]
            print(
                f"   evidence: {evidence['evidence_level']} | {source.get('title', evidence['source_id'])} "
                f"| {evidence['locator']}"
            )
        if scores["matched_terms"]:
            print(f"   matched: {', '.join(scores['matched_terms'])}")
        print()

    if result["limitations"]:
        print("limitations:")
        for limitation in result["limitations"]:
            print(f"  - {limitation}")


def _load_repository(args: argparse.Namespace) -> KnowledgeRepository:
    settings = Settings.from_env(getattr(args, "root", None))
    return KnowledgeRepository.load(settings.root)


def _service(args: argparse.Namespace) -> RetrievalService:
    settings = Settings.from_env(getattr(args, "root", None))
    repository = KnowledgeRepository.load(settings.root)
    if getattr(args, "embedder", None):
        settings = Settings(
            root=settings.root,
            embedder=args.embedder,
            model_name=settings.model_name,
            keyword_weight=settings.keyword_weight,
            semantic_weight=settings.semantic_weight,
        )
    return RetrievalService(repository, settings=settings)


def _filters_from_args(args: argparse.Namespace) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if args.experience_type:
        filters["experience_type"] = args.experience_type
    if args.knowledge_status:
        filters["knowledge_status"] = args.knowledge_status
    if args.tag:
        filters["tags"] = args.tag
    if args.all_tag:
        filters["all_tags"] = args.all_tag
    if args.source:
        filters["source_id"] = args.source
    if args.source_type:
        filters["source_type"] = args.source_type
    if args.min_evidence:
        filters["min_evidence_level"] = args.min_evidence
    if args.network_condition:
        filters["network_condition"] = args.network_condition
    if args.suitable_condition:
        filters["suitable_condition"] = args.suitable_condition
    if args.has_failure_modes is not None:
        filters["has_failure_modes"] = args.has_failure_modes
    return filters


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        repository = _load_repository(args)
    except KnowledgeError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    report = repository.report
    print(
        f"OK: {len(repository.experiences)} experiences, "
        f"{len(repository.sources)} sources, {len(repository.relations)} relations"
    )
    for warning in report.warnings:
        print(f"[WARN] {warning.code} @ {warning.location}: {warning.message}")
    if args.strict and report.warnings:
        print("strict mode: warnings are treated as failures", file=sys.stderr)
        return 1
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    try:
        repository = _load_repository(args)
    except KnowledgeError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    report = check_repository(repository)
    if args.json:
        _dump(report.to_dict())
    else:
        print(
            f"quality: {len(report.errors)} errors, {len(report.warnings)} warnings, "
            f"average completeness={report.average_completeness:.2f}"
        )
        for issue in report.issues:
            print(
                f"[{issue.severity.upper()}] {issue.code} @ {issue.experience_id}: "
                f"{issue.message}"
            )
        print("\ncompleteness:")
        for experience_id, score in sorted(report.completeness.items()):
            print(f"  {experience_id}: {score:.2f}")
    return 1 if report.errors else 0


def cmd_stats(args: argparse.Namespace) -> int:
    try:
        repository = _load_repository(args)
    except KnowledgeError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    _dump(repository.stats())
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    try:
        result = _service(args).search_experiences(
            query, filters=_filters_from_args(args), top_k=args.top_k
        )
    except (KnowledgeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _dump(result)
    else:
        _print_search(result)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    try:
        payload = _service(args).get_experience(args.experience_id)
    except (KnowledgeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _dump(payload)
    else:
        print(f"[{payload['experience_id']}] {payload['title']}")
        print(f"type: {payload['experience_type']}")
        print(f"status: {payload['knowledge_status']}")
        print(f"tags: {', '.join(payload['metadata']['tags'])}")
        print("\nproblem:")
        print(f"  {_truncate(payload['problem_context'].get('underlying_cause', ''), 600)}")
        print("\nmotivation:")
        print(f"  {_truncate(payload['design_rationale'].get('motivation', ''), 600)}")
        print("\nmechanism:")
        print(f"  {_truncate(payload['mechanism'].get('description', ''), 900)}")
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
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    try:
        payload = _service(args).get_source(args.source_id)
    except (KnowledgeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _dump(payload)
    else:
        print(f"[{payload['source_id']}] {payload['title']}")
        print(f"type: {payload['source_type']}  year: {payload['year']}")
        print(f"authors: {', '.join(payload['authors'])}")
        print(f"venue: {payload['venue']}")
        print(f"url: {payload['url']}")
        if payload["doi"]:
            print(f"doi: {payload['doi']}")
        print("\nlocators:")
        for locator in payload["locators"]:
            print(f"  - {locator['type']}: {locator['value']}")
        print("\nused by:")
        for usage in payload["related_from_experiences"]:
            print(
                f"  - {usage['experience_id']} @ {usage['locator']} "
                f"({usage['evidence_level']})"
            )
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    try:
        payload = _service(args).get_related_experiences(args.experience_id)
    except (KnowledgeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _dump(payload)
    else:
        print(f"[{payload['experience_id']}] {payload['title']}")
        for related in payload["related_experiences"]:
            print(
                f"  - {related['experience_id']} [{related['relation']}] "
                f"{related['title']}"
            )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        repository = _load_repository(args)
    except KnowledgeError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for exp in repository.experiences.values():
        print(f"{exp.id}\t{exp.knowledge_status}\t{exp.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cca-kb",
        description="TCP/QUIC congestion-control expert design-experience knowledge base",
    )
    parser.add_argument("--root", help="project root (defaults to CCA_KB_ROOT or cwd)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate schemas and references")
    validate.add_argument("--root", default=argparse.SUPPRESS)
    validate.add_argument(
        "--strict", action="store_true", help="treat warnings as failures"
    )
    validate.set_defaults(func=cmd_validate)

    quality = subparsers.add_parser("quality", help="run the knowledge quality gate")
    quality.add_argument("--root", default=argparse.SUPPRESS)
    quality.add_argument("--json", action="store_true")
    quality.set_defaults(func=cmd_quality)

    stats = subparsers.add_parser("stats", help="show repository statistics")
    stats.add_argument("--root", default=argparse.SUPPRESS)
    stats.set_defaults(func=cmd_stats)

    listing = subparsers.add_parser("list", help="list experience ids and titles")
    listing.add_argument("--root", default=argparse.SUPPRESS)
    listing.set_defaults(func=cmd_list)

    search = subparsers.add_parser("search", help="hybrid search over experiences")
    search.add_argument("query", nargs="+")
    search.add_argument("--root", default=argparse.SUPPRESS)
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--embedder", choices=["auto", "sentence_transformers", "hashing"])
    search.add_argument("--json", action="store_true")
    search.add_argument("--type", dest="experience_type")
    search.add_argument("--status", dest="knowledge_status")
    search.add_argument("--tag", action="append")
    search.add_argument("--all-tag", action="append")
    search.add_argument("--source", action="append")
    search.add_argument("--source-type", action="append")
    search.add_argument("--min-evidence", choices=["A", "B", "C", "D"])
    search.add_argument("--network-condition")
    search.add_argument("--suitable-condition")
    search.add_argument("--has-failure-modes", action=argparse.BooleanOptionalAction)
    search.set_defaults(func=cmd_search)

    get = subparsers.add_parser("get", help="fetch one experience")
    get.add_argument("experience_id")
    get.add_argument("--root", default=argparse.SUPPRESS)
    get.add_argument("--embedder", choices=["auto", "sentence_transformers", "hashing"])
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=cmd_get)

    source = subparsers.add_parser("source", help="fetch one source record")
    source.add_argument("source_id")
    source.add_argument("--root", default=argparse.SUPPRESS)
    source.add_argument("--json", action="store_true")
    source.set_defaults(func=cmd_source)

    related = subparsers.add_parser("related", help="fetch related experiences")
    related.add_argument("experience_id")
    related.add_argument("--root", default=argparse.SUPPRESS)
    related.add_argument("--json", action="store_true")
    related.set_defaults(func=cmd_related)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KnowledgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
