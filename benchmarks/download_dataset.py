"""Download publicly distributable documents for the M0 benchmark set.

Sources are restricted to officially published government template documents,
arXiv papers, listed-company disclosures, Apache-2.0 test fixtures, and
pre-1930 public-domain scans from the Internet Archive. Downloaded files stay
in the git-ignored inputs/ directory; the manifest records source URL,
download date, and SHA256 for every file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .scaffold_dataset import CATEGORIES, CategorySpec

_SPEC_BY_KEY: dict[str, CategorySpec] = {spec.key: spec for spec in CATEGORIES}
CATEGORY_ORDER: tuple[str, ...] = tuple(spec.key for spec in CATEGORIES)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE

MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
MIN_DOWNLOAD_BYTES = 1024
DOWNLOAD_WORKERS = 4

_ALLOWED_EXTS: dict[str, frozenset[str]] = {
    "contracts": frozenset({".pdf", ".docx"}),
    "academic_papers": frozenset({".pdf"}),
    "financial_reports": frozenset({".pdf"}),
    "scanned_documents": frozenset({".pdf"}),
    "presentations": frozenset({".pptx"}),
    "spreadsheets": frozenset({".xlsx", ".xls", ".xlsm"}),
    "official_red_header": frozenset({".pdf"}),
    "formula_heavy": frozenset({".pdf"}),
}

_LANGUAGE_BY_CATEGORY: dict[str, str] = {
    "academic_papers": "en",
    "presentations": "en",
    "spreadsheets": "en",
    "formula_heavy": "en",
}

_LN_CONTRACTS_URL = (
    "https://scjg.ln.gov.cn/scjdglj/fw/wyb/wljyjg/htsfwbk/gjbwhtsfwb/index.shtml"
)
_LX_CONTRACTS_URL = (
    "https://scjg.linxia.gov.cn/scjg/zfxxgk/fdzdgknr/zdlyxxgk/XFWQ/htwbk/index.html"
)
_SD_CONTRACTS_URL = "http://amr.shandong.gov.cn/col/col189533/index.html"

POI_RAW_BASE = "https://raw.githubusercontent.com/apache/poi/trunk/"


@dataclass(frozen=True)
class Candidate:
    """One downloadable document candidate."""

    category: str
    url: str
    title: str
    ext: str = ""


@dataclass(frozen=True)
class SavedDoc:
    """One downloaded document registered in the manifest."""

    id: str
    category: str
    filename: str
    source_url: str
    sha256: str
    title: str
    downloaded_at: str


def _http_get(url: str, *, timeout: float = 120.0, retries: int = 3) -> bytes:
    """Fetch a URL with retries; fall back to unverified TLS for old gov sites."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload: bytes = response.read()
                return payload
        except ssl.SSLError as exc:
            last_error = exc
            request = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout, context=_UNVERIFIED_SSL) as response:
                fallback: bytes = response.read()
                return fallback
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if "HTTP Error" in str(exc) and re.search(r"HTTP Error 4\d\d", str(exc)):
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed after {retries} attempts: {url}: {last_error}")


def _http_get_text(url: str) -> str:
    """Fetch a URL and decode as UTF-8 with GBK fallback for legacy gov sites."""
    data = _http_get(url)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="ignore")


def _http_post_form(url: str, fields: dict[str, str], *, timeout: float = 60.0) -> bytes:
    """POST urlencoded form data and return the raw response."""
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body: bytes = response.read()
        return body


def _strip_tags(text: str) -> str:
    """Remove HTML tags and collapse whitespace from a title."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


def _candidate_ext(candidate: Candidate) -> str | None:
    """Resolve the file extension for a candidate, or None if unsupported."""
    if candidate.ext:
        return candidate.ext.lower()
    suffix = Path(urllib.parse.urlparse(candidate.url).path).suffix.lower()
    return suffix or None


def _sha256(data: bytes) -> str:
    """Return the hex SHA256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def _download_candidate(candidate: Candidate) -> bytes:
    """Download one candidate with size guards."""
    data = _http_get(candidate.url)
    if len(data) < MIN_DOWNLOAD_BYTES:
        raise ValueError(f"response too small ({len(data)} bytes): {candidate.url}")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"response too large ({len(data)} bytes): {candidate.url}")
    return data


def collect_arxiv(category: str, query: str, limit: int = 45) -> list[Candidate]:
    """Collect arXiv paper PDF candidates through the export API."""
    api_url = (
        "http://export.arxiv.org/api/query?search_query="
        + urllib.parse.quote(query)
        + f"&max_results={limit}"
    )
    xml = _http_get_text(api_url)
    ids = re.findall(r"<id>http://arxiv.org/abs/([^<]+)</id>", xml)
    return [
        Candidate(category, f"https://arxiv.org/pdf/{arxiv_id}", f"arXiv:{arxiv_id}", ".pdf")
        for arxiv_id in ids
    ]


def collect_poi(category: str, directory: str, exts: tuple[str, ...], limit: int = 45) -> list[Candidate]:
    """Collect Apache POI test-fixture documents via the GitHub contents API."""
    api_url = f"https://api.github.com/repos/apache/poi/contents/{directory}?ref=trunk"
    entries = json.loads(_http_get_text(api_url))
    candidates: list[Candidate] = []
    for entry in entries:
        name = str(entry.get("name", ""))
        size = int(entry.get("size", 0))
        if not name.lower().endswith(exts) or not 1024 <= size <= 5 * 1024 * 1024:
            continue
        download_url = str(entry.get("download_url", ""))
        if not download_url:
            continue
        candidates.append(
            Candidate(category, download_url, f"apache/poi {directory}/{name}", Path(name).suffix.lower())
        )
        if len(candidates) >= limit:
            break
    return candidates


def collect_cninfo(category: str, max_stocks: int = 10, per_stock: int = 8) -> list[Candidate]:
    """Collect listed-company periodic report PDFs from cninfo disclosures."""
    stock_list = json.loads(_http_get_text("http://www.cninfo.com.cn/new/data/szse_stock.json"))
    stocks = [
        item
        for item in stock_list.get("stockList", [])
        if str(item.get("code", ""))[:3] in {"000", "002", "300"}
    ][:max_stocks]
    candidates: list[Candidate] = []
    for stock in stocks:
        code = str(stock.get("code", ""))
        org_id = str(stock.get("orgId", ""))
        name = str(stock.get("zwjc", code))
        for category_param in ("category_ndbg_szsh", "category_bndbg_szsh"):
            try:
                response = json.loads(
                    _http_post_form(
                        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
                        {
                            "pageNum": "1",
                            "pageSize": str(per_stock),
                            "column": "szse",
                            "tabName": "fulltext",
                            "stock": f"{code},{org_id}",
                            "seDate": "2019-01-01~2026-12-31",
                            "category": category_param,
                        },
                    )
                )
            except (RuntimeError, json.JSONDecodeError):
                continue
            for announcement in response.get("announcements") or []:
                adjunct = str(announcement.get("adjunctUrl", ""))
                if not adjunct.lower().endswith(".pdf"):
                    continue
                title = _strip_tags(str(announcement.get("announcementTitle", adjunct)))
                candidates.append(
                    Candidate(
                        category,
                        f"http://static.cninfo.com.cn/{adjunct}",
                        f"{name} {title}",
                        ".pdf",
                    )
                )
    return candidates


def _ia_search(query: str, rows: int) -> list[dict[str, object]]:
    """Run an Internet Archive advancedsearch query and return docs."""
    search_url = (
        "https://archive.org/advancedsearch.php?q="
        + urllib.parse.quote(query)
        + "&fl%5B%5D=identifier&fl%5B%5D=item_size"
        + f"&rows={rows}&sort%5B%5D=item_size+asc&output=json"
    )
    payload = json.loads(_http_get_text(search_url))
    return list(payload.get("response", {}).get("docs", []))


def _ia_pdf_candidate(category: str, identifier: str, max_pdf_bytes: int) -> Candidate | None:
    """Pick the main scan PDF (not the OCR text overlay) from an IA item."""
    try:
        meta = json.loads(_http_get_text(f"https://archive.org/metadata/{identifier}"))
    except (RuntimeError, json.JSONDecodeError):
        return None
    best: tuple[str, int] | None = None
    for file_info in meta.get("files", []):
        name = str(file_info.get("name", ""))
        if not name.lower().endswith(".pdf") or name.lower().endswith("_text.pdf"):
            continue
        try:
            size = int(file_info.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if 0 < size <= max_pdf_bytes and (best is None or size < best[1]):
            best = (name, size)
    if best is None:
        return None
    name = best[0]
    url = "https://archive.org/download/" + identifier + "/" + urllib.parse.quote(name)
    return Candidate(category, url, f"Internet Archive:{identifier}", ".pdf")


def collect_ia_documents(
    category: str,
    query: str,
    *,
    max_search: int = 80,
    max_candidates: int = 45,
    max_pdf_mb: int = 50,
) -> list[Candidate]:
    """Collect public-domain IA scan PDFs matching a search query."""
    candidates: list[Candidate] = []
    for doc in _ia_search(query, max_search):
        identifier = str(doc.get("identifier", ""))
        if not identifier:
            continue
        time.sleep(0.2)
        candidate = _ia_pdf_candidate(category, identifier, max_pdf_mb * 1024 * 1024)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _extract_linxia_download_links(html: str) -> list[tuple[str, str, str]]:
    """Extract (url, title, ext) triples from jpaas document download links."""
    results: list[tuple[str, str, str]] = []
    for match in re.finditer(r'href="(/api-gateway/[^"]+download[^"]*)"', html):
        raw = match.group(1)
        url = "https://scjg.linxia.gov.cn" + raw.replace("&amp;", "&")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        file_name = (query.get("fileName") or [""])[0]
        title = re.sub(r"^附件\d+[：:]", "", file_name)
        ext = Path(file_name).suffix.lower()
        if ext in _ALLOWED_EXTS["contracts"]:
            results.append((url, title, ext))
    return results


def collect_samr_contracts(category: str, pages: int = 2, page_size: int = 20) -> list[Candidate]:
    """Collect national contract template PDFs from the SAMR template library API."""
    candidates: list[Candidate] = []
    for page in range(1, pages + 1):
        try:
            payload = json.loads(
                _http_get_text(
                    "https://htsfwb.samr.gov.cn/api/content/SearchTemplates"
                    + f"?page={page}&pageSize={page_size}"
                )
            )
        except (RuntimeError, json.JSONDecodeError):
            continue
        for item in payload.get("Data") or []:
            template_id = str(item.get("Id", ""))
            title = _strip_tags(str(item.get("Title", "")))
            if not template_id:
                continue
            url = f"https://htsfwb.samr.gov.cn/api/File/DownTemplate?id={template_id}&type=2"
            candidates.append(Candidate(category, url, f"市场监管总局 {title}", ".pdf"))
    return candidates


def collect_contracts(category: str, max_art_pages: int = 30) -> list[Candidate]:
    """Collect government-published contract template documents (PDF/DOCX)."""
    candidates: list[Candidate] = []
    candidates.extend(collect_samr_contracts(category, pages=3))

    liaoning = _http_get_text(_LN_CONTRACTS_URL)
    for viewer_file in re.findall(r"viewer\.html\?file=([^\"&]+)", liaoning):
        url = urllib.parse.urljoin(_LN_CONTRACTS_URL, urllib.parse.unquote(viewer_file))
        candidates.append(Candidate(category, url, "辽宁市场监管-合同示范文本", ".pdf"))
    for href in re.findall(r'href="([^"]+\.docx?)"', liaoning, re.I):
        url = urllib.parse.urljoin(_LN_CONTRACTS_URL, href)
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext == ".docx":
            candidates.append(Candidate(category, url, "辽宁市场监管-合同示范文本", ext))

    linxia = _http_get_text(_LX_CONTRACTS_URL)
    art_links = [
        "https://scjg.linxia.gov.cn" + link.replace("&amp;", "&")
        for link in re.findall(r'href="(/scjg/[^"]+/art/[^"]+\.html)"', linxia)
    ][:max_art_pages]
    pages = [linxia] + [_http_get_text(url) for url in art_links]
    for page in pages:
        for url, title, ext in _extract_linxia_download_links(page):
            candidates.append(Candidate(category, url, f"临夏市市场监管 {title}", ext))

    try:
        shandong_index = _http_get_text(_SD_CONTRACTS_URL)
        shandong_arts = re.findall(r'href="(http://amr\.shandong\.gov\.cn/art/[^"]+\.html)"', shandong_index)
        for url in shandong_arts[:max_art_pages]:
            art_html = _http_get_text(url)
            for href in re.findall(r'href="([^"]+\.(?:pdf|docx)[^"]*)"', art_html, re.I):
                file_url = urllib.parse.urljoin(url, href.replace("&amp;", "&"))
                ext = Path(urllib.parse.urlparse(file_url).path).suffix.lower()
                if ext in _ALLOWED_EXTS[category]:
                    candidates.append(Candidate(category, file_url, "山东市场监管-合同示范文本", ext))
    except RuntimeError:
        pass

    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        if candidate.url not in seen:
            seen.add(candidate.url)
            unique.append(candidate)
    return unique


_COLLECTORS: dict[str, Callable[[], list[Candidate]]] = {
    "contracts": lambda: collect_contracts("contracts"),
    "academic_papers": lambda: collect_arxiv("academic_papers", "cat:cs.CL"),
    "financial_reports": lambda: collect_cninfo("financial_reports"),
    "scanned_documents": lambda: collect_ia_documents(
        "scanned_documents",
        'language:chi AND mediatype:texts AND format:"Text PDF" AND year:[1800 TO 1929]',
    ),
    "presentations": lambda: collect_poi("presentations", "test-data/slideshow", (".pptx",)),
    "spreadsheets": lambda: collect_poi(
        "spreadsheets", "test-data/spreadsheet", (".xlsx", ".xls", ".xlsm")
    ),
    "official_red_header": lambda: collect_ia_documents(
        "official_red_header",
        "identifier:gmzfgb-*",
        max_search=120,
        max_pdf_mb=60,
    ),
    "formula_heavy": lambda: collect_arxiv("formula_heavy", "cat:math.AP"),
}


def build_plan(categories: tuple[str, ...]) -> dict[str, list[Candidate]]:
    """Build the download plan: per-category candidate lists.

    Quotas are applied during download; collectors overshoot on purpose so
    failed downloads do not starve a category.
    """
    return {category: _COLLECTORS[category]() for category in categories}


def _next_index(existing_names: set[str], category: str) -> int:
    """Return the next free numeric suffix for a category."""
    pattern = re.compile(rf"^{category}_(\d{{3}})\.")
    indices = [int(match.group(1)) for name in existing_names if (match := pattern.match(name))]
    return max(indices, default=0) + 1


def download_category(
    category: str,
    candidates: list[Candidate],
    quota: int,
    dataset_dir: Path,
    seen_hashes: set[str],
    known_rows: dict[str, dict[str, object]],
) -> list[SavedDoc]:
    """Download candidates for one category until the quota is met.

    Existing files count toward the quota so reruns resume instead of
    redownloading. Rows for existing files are reused from the previous
    manifest when the recorded hash still matches.
    """
    inputs_dir = dataset_dir / "inputs" / category
    inputs_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(path for path in inputs_dir.iterdir() if path.is_file())
    saved: list[SavedDoc] = []
    next_index = _next_index({path.name for path in existing}, category)

    for path in existing:
        data = path.read_bytes()
        digest = _sha256(data)
        seen_hashes.add(digest)
        row = known_rows.get(f"inputs/{category}/{path.name}")
        saved.append(
            SavedDoc(
                id=path.stem,
                category=category,
                filename=path.name,
                source_url=str(row.get("source_url", "unknown")) if row else "unknown",
                sha256=digest,
                title=str(row.get("title", path.stem)) if row else path.stem,
                downloaded_at=str(row.get("downloaded_at", "")) if row else "",
            )
        )

    def try_candidate(candidate: Candidate) -> tuple[Candidate, bytes | None, str]:
        try:
            ext = _candidate_ext(candidate)
            if ext is None or ext not in _ALLOWED_EXTS[category]:
                return candidate, None, "unsupported extension"
            return candidate, _download_candidate(candidate), ""
        except Exception as exc:  # noqa: BLE001 - collect any per-file failure
            return candidate, None, str(exc)

    chunk_size = DOWNLOAD_WORKERS * 2
    for start in range(0, len(candidates), chunk_size):
        if len(saved) >= quota:
            break
        chunk = candidates[start : start + chunk_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            for candidate, file_bytes, error in pool.map(try_candidate, chunk):
                if file_bytes is None:
                    print(f"  skip {candidate.url}: {error}")
                    continue
                digest = _sha256(file_bytes)
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                ext = _candidate_ext(candidate) or ".pdf"
                doc_id = f"{category}_{next_index:03d}"
                next_index += 1
                filename = f"{doc_id}{ext}"
                (inputs_dir / filename).write_bytes(file_bytes)
                saved.append(
                    SavedDoc(
                        id=doc_id,
                        category=category,
                        filename=filename,
                        source_url=candidate.url,
                        sha256=digest,
                        title=candidate.title,
                        downloaded_at=datetime.now(UTC).strftime("%Y-%m-%d"),
                    )
                )
                if len(saved) >= quota:
                    break
    return saved


def build_manifest_row(doc: SavedDoc) -> dict[str, object]:
    """Build one manifest row for a saved document."""
    spec = _SPEC_BY_KEY[doc.category]
    return {
        "id": doc.id,
        "category": doc.category,
        "category_label": spec.label,
        "input": f"inputs/{doc.category}/{doc.filename}",
        "ground_truth": f"ground_truth/{doc.category}/{doc.id}.md",
        "features": list(spec.features),
        "language": _LANGUAGE_BY_CATEGORY.get(doc.category, "zh-Hans"),
        "status": "ready",
        "title": doc.title,
        "source_url": doc.source_url,
        "sha256": doc.sha256,
        "downloaded_at": doc.downloaded_at,
    }


def _read_manifest_rows(manifest_path: Path) -> dict[str, dict[str, object]]:
    """Read the existing manifest into a mapping keyed by relative input path."""
    known_rows: dict[str, dict[str, object]] = {}
    if manifest_path.is_file():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                known_rows[str(row.get("input", ""))] = row
    return known_rows


def _write_manifest_rows(
    dataset_dir: Path,
    rows_by_rel: dict[str, dict[str, object]],
) -> None:
    """Rewrite the manifest from every file on disk plus known metadata.

    Files without any known metadata (only possible for interrupted partial
    runs) still get a row with a recomputed hash and unknown source.
    """
    inputs_root = dataset_dir / "inputs"
    rows: list[dict[str, object]] = []
    for category in CATEGORY_ORDER:
        category_dir = inputs_root / category
        if not category_dir.is_dir():
            continue
        for path in sorted(category_dir.iterdir()):
            if not path.is_file():
                continue
            relative = f"inputs/{category}/{path.name}"
            row = rows_by_rel.get(relative)
            digest = _sha256(path.read_bytes())
            if row is None or str(row.get("sha256", "")) != digest:
                row = {
                    "id": path.stem,
                    "category": category,
                    "category_label": _SPEC_BY_KEY[category].label,
                    "input": relative,
                    "ground_truth": f"ground_truth/{category}/{path.stem}.md",
                    "features": list(_SPEC_BY_KEY[category].features),
                    "language": _LANGUAGE_BY_CATEGORY.get(category, "zh-Hans"),
                    "status": "ready",
                    "title": path.stem,
                    "source_url": "unknown",
                    "sha256": digest,
                    "downloaded_at": "",
                }
            rows.append(row)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dataset_dir / "00_manifest.jsonl"
    payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
    temp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(manifest_path)


def download_dataset(
    dataset_dir: Path,
    *,
    quota: int = 25,
    categories: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Download documents for the selected categories and update the manifest.

    The manifest is rewritten after every category so partial runs never lose
    metadata for the categories downloaded earlier.
    """
    manifest_path = dataset_dir / "00_manifest.jsonl"
    known_rows = _read_manifest_rows(manifest_path)

    selected = categories or CATEGORY_ORDER
    ordered = tuple(category for category in CATEGORY_ORDER if category in selected)
    plan = build_plan(ordered)
    seen_hashes: set[str] = {str(row.get("sha256", "")) for row in known_rows.values()} - {""}
    rows_by_rel = dict(known_rows)
    counts: dict[str, int] = {}
    for category in ordered:
        print(f"[{category}] candidates: {len(plan[category])}")
        docs = download_category(category, plan[category], quota, dataset_dir, seen_hashes, known_rows)
        counts[category] = len(docs)
        for doc in docs:
            rows_by_rel[f"inputs/{category}/{doc.filename}"] = build_manifest_row(doc)
        _write_manifest_rows(dataset_dir, rows_by_rel)
        print(f"[{category}] saved: {len(docs)}")
    return counts


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("benchmarks/dataset"))
    parser.add_argument("--quota", type=int, default=25, help="target documents per category")
    parser.add_argument(
        "--categories",
        nargs="*",
        choices=CATEGORY_ORDER,
        help="restrict the run to these categories",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    args = build_parser().parse_args()
    if args.quota < 1:
        raise ValueError("--quota must be >= 1")
    counts = download_dataset(args.dataset_dir, quota=args.quota, categories=args.categories)
    print(json.dumps(counts, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()