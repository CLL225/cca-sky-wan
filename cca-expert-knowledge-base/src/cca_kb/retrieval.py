"""LLM-facing retrieval service.

Results always carry the source locator, evidence level and knowledge
status.  When a result set contains inferences or weak evidence, the
response says so explicitly in ``limitations`` so an LLM cannot mistake a
knowledge-base inference for a source-reported fact.
"""

from __future__ import annotations

from typing import Any

from cca_kb.config import Settings
from cca_kb.embeddings import build_embedder
from cca_kb.index import KnowledgeIndex, SearchHit
from cca_kb.models import DesignExperience, EvidenceRef
from cca_kb.repository import KnowledgeRepository

EVIDENCE_WEAK = {"C", "D"}


def _source_brief(repository: KnowledgeRepository, source_id: str) -> dict[str, Any]:
    source = repository.sources.get(source_id)
    if source is None:
        return {"source_id": source_id, "missing": True}
    return {
        "source_id": source.source_id,
        "title": source.title,
        "source_type": source.source_type,
        "authors": source.authors,
        "year": source.year,
        "url": source.url,
        "doi": source.doi,
        "venue": source.venue,
        "version": source.version,
        "locators": [locator.model_dump() for locator in source.locators],
    }


def _evidence_payload(
    repository: KnowledgeRepository, evidence: EvidenceRef
) -> dict[str, Any]:
    payload = evidence.model_dump()
    payload["source"] = _source_brief(repository, evidence.source_id)
    return payload


def _related_payload(
    repository: KnowledgeRepository, exp: DesignExperience
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for relation in repository.relations:
        if relation.from_id == exp.id:
            other_id = relation.to_id
            direction = "outgoing"
        elif relation.to_id == exp.id:
            other_id = relation.from_id
            direction = "incoming"
        else:
            continue
        key = (other_id, relation.relation)
        if key in seen:
            continue
        seen.add(key)
        other = repository.experiences.get(other_id)
        payload.append(
            {
                "experience_id": other_id,
                "title": other.title if other else "",
                "relation": relation.relation,
                "direction": direction,
                "note": relation.note,
                "knowledge_status": relation.knowledge_status,
            }
        )

    linked = {item["experience_id"] for item in payload}
    for related_id in exp.related_experiences:
        if related_id in linked:
            continue
        other = repository.experiences.get(related_id)
        payload.append(
            {
                "experience_id": related_id,
                "title": other.title if other else "",
                "relation": "related_to",
                "direction": "unspecified",
                "note": "",
                "knowledge_status": "source_reported",
            }
        )
    return payload


def _experience_payload(
    repository: KnowledgeRepository,
    hit: SearchHit | DesignExperience,
) -> dict[str, Any]:
    if isinstance(hit, SearchHit):
        exp = hit.experience
    else:
        exp = hit

    payload = exp.model_dump()
    payload["experience_id"] = payload.pop("id")
    payload["evidence"] = [
        _evidence_payload(repository, evidence) for evidence in exp.evidence
    ]
    payload["related_experiences"] = _related_payload(repository, exp)
    payload["sources"] = {
        evidence.source_id: _source_brief(repository, evidence.source_id)
        for evidence in exp.evidence
    }
    if isinstance(hit, SearchHit):
        payload["retrieval"] = {
            "score": hit.score,
            "keyword_score": hit.keyword_score,
            "semantic_score": hit.semantic_score,
            "semantic_cosine": hit.semantic_cosine,
            "title_boost": hit.title_boost,
            "matched_terms": hit.matched_terms,
        }
    return payload


class RetrievalService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        settings: Settings | None = None,
        embedder: Any | None = None,
    ):
        self.repository = repository
        self.settings = settings or Settings.from_env(repository.root)
        self.embedder = embedder or build_embedder(
            self.settings.embedder, self.settings.model_name
        )
        self.index = KnowledgeIndex(
            repository,
            self.embedder,
            keyword_weight=self.settings.keyword_weight,
            semantic_weight=self.settings.semantic_weight,
        )

    # ------------------------------------------------------------------ API
    def search_experiences(
        self,
        query: str,
        filters: dict | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        hits = self.index.search(query, filters=filters, top_k=top_k)
        experiences = [_experience_payload(self.repository, hit) for hit in hits]

        related: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in experiences:
            for relation in item["related_experiences"]:
                related_id = relation["experience_id"]
                if related_id in seen:
                    continue
                seen.add(related_id)
                related.append(relation)

        limitations = self._limitations(hits, query, filters)
        return {
            "query": query,
            "filters": filters or {},
            "top_k": top_k,
            "retrieval": {
                "backend": self.embedder.name,
                "semantic": bool(getattr(self.embedder, "is_semantic", False)),
                "keyword_weight": self.settings.keyword_weight,
                "semantic_weight": self.settings.semantic_weight,
                "returned": len(experiences),
            },
            "experiences": experiences,
            "related_experiences": related,
            "limitations": limitations,
        }

    def get_experience(self, experience_id: str) -> dict[str, Any]:
        exp = self.repository.experience(experience_id)
        payload = _experience_payload(self.repository, exp)
        payload["evidence_summary"] = [
            {
                "source_id": evidence.source_id,
                "source_title": self.repository.sources[evidence.source_id].title,
                "locator": evidence.locator,
                "evidence_level": evidence.evidence_level,
                "supports": evidence.supports,
            }
            for evidence in exp.evidence
        ]
        return payload

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = self.repository.source(source_id)
        payload = source.model_dump()
        payload["related_experiences"] = [
            {
                "experience_id": related_id,
                "title": (
                    self.repository.experiences[related_id].title
                    if related_id in self.repository.experiences
                    else ""
                ),
            }
            for related_id in source.related_experiences
        ]
        payload["related_from_experiences"] = [
            {
                "experience_id": exp.id,
                "title": exp.title,
                "locator": evidence.locator,
                "evidence_level": evidence.evidence_level,
                "supports": evidence.supports,
            }
            for exp in self.repository.experiences.values()
            for evidence in exp.evidence
            if evidence.source_id == source_id
        ]
        return payload

    def get_related_experiences(self, experience_id: str) -> dict[str, Any]:
        exp = self.repository.experience(experience_id)
        return {
            "experience_id": exp.id,
            "title": exp.title,
            "related_experiences": _related_payload(self.repository, exp),
        }

    # ---------------------------------------------------------- limitations
    def _limitations(
        self,
        hits: list[SearchHit],
        query: str,
        filters: dict | None,
    ) -> list[str]:
        limitations: list[str] = []

        if not hits:
            limitations.append(
                "没有找到与查询匹配的经验；可以放宽元数据过滤条件，或换用更接近领域术语的表述。"
            )
            return limitations

        if not getattr(self.embedder, "is_semantic", False):
            limitations.append(
                "当前向量后端是词法哈希嵌入（hashing），只能近似匹配用词，"
                "不等价于语义模型；如需跨语言语义检索，请启用 sentence-transformers 后端。"
            )

        inference_ids = [
            hit.experience.id
            for hit in hits
            if hit.experience.knowledge_status == "inference"
        ]
        if inference_ids:
            limitations.append(
                "结果包含知识库推论（inference）："
                + "、".join(inference_ids)
                + "。这些内容不是原作者明确结论，使用前应核对证据并标注为推论。"
            )

        weak_ids = [
            hit.experience.id
            for hit in hits
            if min(e.evidence_level for e in hit.experience.evidence) in EVIDENCE_WEAK
        ]
        if weak_ids:
            limitations.append(
                "结果包含仅有 C/D 级证据的经验："
                + "、".join(weak_ids)
                + "。C 表示单一来源间接证据或有限实验支持，D 表示缺少直接验证。"
            )

        if hits[0].score < 0.25:
            limitations.append(
                f"最高相关度得分较低（{hits[0].score:.2f}），检索结果可能不相关，"
                "建议补充问题背景或使用更具体的拥塞控制术语。"
            )

        if filters:
            limitations.append(
                "检索已应用元数据过滤；未出现在结果中的经验不代表知识库没有相关记录。"
            )

        return limitations
