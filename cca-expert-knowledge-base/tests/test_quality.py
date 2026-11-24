from cca_kb.quality import check_repository


def test_shipped_knowledge_has_no_blocking_quality_errors(repository):
    report = check_repository(repository)
    assert report.errors == []


def test_completeness_is_high(repository):
    report = check_repository(repository)
    assert report.average_completeness >= 0.9
    for experience_id, score in report.completeness.items():
        assert score >= 0.75, experience_id


def test_no_unknown_taxonomy_tags(repository):
    report = check_repository(repository)
    assert not [issue for issue in report.issues if issue.code == "TAXONOMY-UNKNOWN"]


def test_no_duplicate_titles(repository):
    report = check_repository(repository)
    assert not [issue for issue in report.issues if issue.code == "W-DUPLICATE-TITLE"]


def test_every_record_is_linked(repository):
    report = check_repository(repository)
    assert not [issue for issue in report.issues if issue.code == "W-NO-RELATED"]


def test_sources_have_locators_and_links(repository):
    report = check_repository(repository)
    assert not [
        issue
        for issue in report.issues
        if issue.code in {"W-SOURCE-NO-LOCATOR", "W-SOURCE-NO-URL"}
    ]


def test_knowledge_status_distribution(repository):
    statuses = {exp.knowledge_status for exp in repository.experiences.values()}
    assert statuses <= {"source_reported", "cross_source_synthesis", "inference"}
    assert "source_reported" in statuses


def test_quality_report_serializes(repository):
    report = check_repository(repository)
    payload = report.to_dict()
    assert "errors" in payload
    assert "warnings" in payload
    assert payload["average_completeness"] >= 0.9
