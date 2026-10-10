"""Tests for the benchmark dataset downloader helpers."""

from __future__ import annotations

from pathlib import Path

from benchmarks.download_dataset import (
    Candidate,
    _candidate_ext,
    _extract_linxia_download_links,
    _next_index,
    _strip_tags,
    build_manifest_row,
)


def test_candidate_ext_prefers_explicit() -> None:
    candidate = Candidate("contracts", "https://example.com/a", "t", ".pdf")
    assert _candidate_ext(candidate) == ".pdf"


def test_candidate_ext_falls_back_to_url() -> None:
    candidate = Candidate("contracts", "https://example.com/a.docx?query=1", "t")
    assert _candidate_ext(candidate) == ".docx"


def test_candidate_ext_unknown_returns_none() -> None:
    candidate = Candidate("contracts", "https://example.com/download", "t")
    assert _candidate_ext(candidate) is None


def test_strip_tags() -> None:
    assert _strip_tags("<em>2024</em>年度  报告") == "2024年度 报告"


def test_next_index() -> None:
    assert _next_index({"contracts_001.pdf", "contracts_003.docx"}, "contracts") == 4
    assert _next_index(set[str](), "contracts") == 1


def test_build_manifest_row_fields() -> None:
    from benchmarks.download_dataset import SavedDoc

    doc = SavedDoc(
        id="contracts_001",
        category="contracts",
        filename="contracts_001.pdf",
        source_url="https://example.com",
        sha256="abc",
        title="测试合同",
        downloaded_at="2026-10-10",
    )
    row = build_manifest_row(doc)
    assert row["id"] == "contracts_001"
    assert row["input"] == "inputs/contracts/contracts_001.pdf"
    assert row["ground_truth"] == "ground_truth/contracts/contracts_001.md"
    assert row["status"] == "ready"
    assert row["sha256"] == "abc"
    assert "tables" in row["features"]


def test_extract_linxia_download_links(tmp_path: Path) -> None:
    html = (
        '<a href="/api-gateway/jpaas-web-server/front/document/download'
        '?fileUrl=abc&fileName=%E9%99%84%E4%BB%B61%EF%BC%9A%E6%97%85%E6%B8%B8%E5%90%88%E5%90%8C'
        '%EF%BC%88%E7%A4%BA%E8%8C%83%E6%96%87%E6%9C%AC%EF%BC%89.pdf">x</a>'
    )
    links = _extract_linxia_download_links(html)
    assert len(links) == 1
    url, title, ext = links[0]
    assert url.startswith("https://scjg.linxia.gov.cn/api-gateway/")
    assert title == "旅游合同（示范文本）.pdf"
    assert ext == ".pdf"