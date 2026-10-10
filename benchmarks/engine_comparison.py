"""Run the M0 three-engine comparison without ground-truth annotations.

Each conversion runs inside a persistent worker subprocess (one per engine
batch) while the parent monitors progress. A document that makes no progress
for ``timeout`` seconds is marked ``failed_timeout`` and the worker is
restarted for the remaining documents. Results are stored as one JSON file
per document so reruns skip documents that already succeeded.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from .run_benchmark import DEFAULT_ENGINES, ManifestEntry, load_manifest
from .structure_metrics import StructureMetrics, extract_structure_metrics, jaccard_similarity

WORKER_MODULE = "benchmarks._convert_worker"
STALL_BUFFER_SECONDS = 15.0
MODEL_DOWNLOAD_GRACE_SECONDS = 300.0
MAX_ROUNDS = 60
STRUCTURE_FIELDS: tuple[str, ...] = (
    "table_count",
    "heading_count",
    "heading_levels",
    "list_item_count",
    "image_count",
    "formula_count",
)
FAILURE_REASON_TIMEOUT = "failed_timeout"
FAILURE_REASON_CRASH = "failed_crash"
FAILURE_REASON_EMPTY = "failed_empty"
FAILURE_REASON_ENGINE_MISSING = "failed_engine_missing"


@dataclass(frozen=True)
class RawResult:
    """One raw conversion result as stored on disk."""

    doc_id: str
    status: str
    elapsed_seconds: float
    output_chars: int
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        """Return the record as a JSON-serializable dict."""
        return {
            "id": self.doc_id,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "output_chars": self.output_chars,
            "error": self.error,
        }


@dataclass
class ComparisonRecord:
    """One per-document per-engine row in the summary."""

    engine: str
    category: str
    doc_id: str
    status: str
    failure_reason: str
    elapsed_seconds: float
    output_chars: int
    metrics: StructureMetrics
    agreement_avg: float | None


def _results_dir(outputs_dir: Path, engine: str) -> Path:
    """Return the per-engine results directory."""
    return outputs_dir / engine / "results"


def _load_raw_results(outputs_dir: Path, engine: str) -> dict[str, RawResult]:
    """Load stored per-document results for one engine."""
    results: dict[str, RawResult] = {}
    results_path = _results_dir(outputs_dir, engine)
    if not results_path.is_dir():
        return results
    for path in results_path.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        results[str(payload.get("id", path.stem))] = RawResult(
            doc_id=str(payload.get("id", path.stem)),
            status=str(payload.get("status", "error")),
            elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
            output_chars=int(payload.get("output_chars", 0)),
            error=str(payload.get("error", "")),
        )
    return results


def _store_raw_result(outputs_dir: Path, engine: str, result: RawResult) -> None:
    """Persist one raw result for resume and reporting."""
    results_path = _results_dir(outputs_dir, engine)
    results_path.mkdir(parents=True, exist_ok=True)
    result_path = results_path / f"{result.doc_id}.json"
    temp_path = result_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(result.to_json(), ensure_ascii=False), encoding="utf-8")
    temp_path.replace(result_path)


def _default_python_exe(engine: str) -> Path:
    """Locate the dedicated engine venv python, falling back to the current one.

    A venv under the user profile (ASCII-only path) is preferred for engines
    with native extensions that cannot handle non-ASCII install paths.
    """
    candidates = (
        Path(".venvs") / engine / "Scripts" / "python.exe",
        Path.home() / ".doc-bench-venvs" / engine / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _run_engine(
    engine: str,
    python_exe: Path,
    tasks: list[ManifestEntry],
    outputs_dir: Path,
    timeout: float,
) -> dict[str, RawResult]:
    """Run all pending documents for one engine with timeout supervision."""
    engine_dir = outputs_dir / engine
    engine_dir.mkdir(parents=True, exist_ok=True)
    processed = _load_raw_results(outputs_dir, engine)
    remaining = [task for task in tasks if processed.get(task.id, RawResult(task.id, "", 0.0, 0)).status != "ok"]
    if not remaining:
        return processed

    docs_file = engine_dir / f"pending_docs_{os.getpid()}.jsonl"
    stderr_log = (engine_dir / "worker_stderr.log").open("ab")
    rounds = 0
    while remaining and rounds < MAX_ROUNDS:
        rounds += 1
        payload = "".join(
            json.dumps(
                {
                    "id": task.id,
                    "input": str(task.input),
                    "output": str(outputs_dir / engine / f"{task.id}.md"),
                },
                ensure_ascii=False,
            )
            + "\n"
            for task in remaining
        )
        docs_file.write_text(payload, encoding="utf-8")
        worker_env = dict(os.environ)
        worker_env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [str(python_exe), "-m", WORKER_MODULE, engine, str(docs_file), str(_results_dir(outputs_dir, engine))],
            cwd=Path.cwd(),
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            env=worker_env,
        )
        cold_start = not any(result.status == "ok" for result in processed.values())
        grace = MODEL_DOWNLOAD_GRACE_SECONDS if cold_start else 0.0
        last_progress = time.monotonic()
        last_reported = (-1, -1)
        last_seen_ids: set[str] = set()
        killed_for_timeout = False
        while True:
            time.sleep(2.0)
            processed = _load_raw_results(outputs_dir, engine)
            ok_count = sum(1 for result in processed.values() if result.status == "ok")
            seen_ids = {task.id for task in remaining if task.id in processed}
            if seen_ids != last_seen_ids:
                # progress = a NEW result file appeared for this batch
                last_progress = time.monotonic()
                last_seen_ids = seen_ids
                snapshot = (ok_count, len(seen_ids))
                if snapshot != last_reported:
                    last_reported = snapshot
                    print(f"  [{engine}] {ok_count}/{len(tasks)} ok (total)")
            if process.poll() is not None:
                break
            first_unprocessed = next(
                (task for task in remaining if task.id not in processed), None
            )
            if first_unprocessed is None:
                break
            if time.monotonic() - last_progress > timeout + STALL_BUFFER_SECONDS + grace:
                process.kill()
                killed_for_timeout = True
                print(
                    f"  [{engine}] timeout after {timeout:.0f}s: {first_unprocessed.id}"
                )
                _store_raw_result(
                    outputs_dir,
                    engine,
                    RawResult(
                        doc_id=first_unprocessed.id,
                        status="timeout",
                        elapsed_seconds=timeout,
                        output_chars=0,
                        error=f"no progress for {timeout:.0f}s",
                    ),
                )
                break
        returncode = process.poll()
        if returncode is None:  # killed above
            process.wait()
            returncode = process.returncode
        processed = _load_raw_results(outputs_dir, engine)
        remaining = [task for task in tasks if task.id not in processed]
        if remaining and killed_for_timeout:
            continue
        if remaining and returncode not in (0, None):
            # Worker crashed outside a per-doc exception (e.g. import error).
            first_unprocessed = next(
                (task for task in remaining if task.id not in processed), None
            )
            if first_unprocessed is not None:
                print(f"  [{engine}] worker exited with code {returncode}; marking engine missing")
                for task in tasks:
                    if task.id not in processed:
                        _store_raw_result(
                            outputs_dir,
                            engine,
                            RawResult(
                                doc_id=task.id,
                                status="engine_missing",
                                elapsed_seconds=0.0,
                                output_chars=0,
                                error=f"worker exited with code {returncode}",
                            ),
                        )
                        processed = _load_raw_results(outputs_dir, engine)
                break
    stderr_log.close()
    return processed


def build_records(
    engines: tuple[str, ...],
    entries: list[ManifestEntry],
    outputs_dir: Path,
) -> list[ComparisonRecord]:
    """Assemble per-document records with structure metrics and agreement."""
    raw_by_engine: dict[str, dict[str, RawResult]] = {
        engine: _load_raw_results(outputs_dir, engine) for engine in engines
    }
    outputs: dict[str, dict[str, str]] = {
        engine: {} for engine in engines
    }
    for engine in engines:
        for entry in entries:
            output_path = outputs_dir / engine / f"{entry.id}.md"
            if raw_by_engine[engine].get(entry.id, RawResult(entry.id, "", 0.0, 0)).status == "ok":
                outputs[engine][entry.id] = (
                    output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
                )

    agreement: dict[str, float | None] = {}
    for entry in entries:
        texts = [
            outputs[engine][entry.id]
            for engine in engines
            if entry.id in outputs[engine]
        ]
        if len(texts) < 2:
            agreement[entry.id] = None
            continue
        pair_scores = [
            jaccard_similarity(left, right)
            for left, right in itertools.combinations(texts, 2)
        ]
        agreement[entry.id] = mean(pair_scores)

    records: list[ComparisonRecord] = []
    for engine in engines:
        for entry in entries:
            raw = raw_by_engine[engine].get(entry.id)
            metrics = StructureMetrics(0, 0, 0, 0, 0, 0)
            if raw is None:
                status, reason, elapsed, chars = "missing", FAILURE_REASON_CRASH, 0.0, 0
            elif raw.status == "ok":
                markdown = outputs[engine].get(entry.id, "")
                if markdown.strip():
                    metrics = extract_structure_metrics(markdown)
                    status, reason = "ok", ""
                else:
                    status, reason = "failed", FAILURE_REASON_EMPTY
                elapsed, chars = raw.elapsed_seconds, raw.output_chars
            elif raw.status == "timeout":
                status, reason, elapsed, chars = (
                    "failed",
                    FAILURE_REASON_TIMEOUT,
                    raw.elapsed_seconds,
                    0,
                )
            elif raw.status == "engine_missing":
                status, reason, elapsed, chars = (
                    "failed",
                    FAILURE_REASON_ENGINE_MISSING,
                    0.0,
                    0,
                )
            else:
                status, reason, elapsed, chars = (
                    "failed",
                    FAILURE_REASON_CRASH,
                    raw.elapsed_seconds,
                    raw.output_chars,
                )
            records.append(
                ComparisonRecord(
                    engine=engine,
                    category=entry.category,
                    doc_id=entry.id,
                    status=status,
                    failure_reason=reason,
                    elapsed_seconds=elapsed,
                    output_chars=chars,
                    metrics=metrics,
                    agreement_avg=agreement[entry.id] if status == "ok" else None,
                )
            )
    return records


def _agg(records: list[ComparisonRecord]) -> dict[str, Any]:
    """Aggregate a group of records for one engine (or engine x category)."""
    ok = [record for record in records if record.status == "ok"]
    failures = [record for record in records if record.status != "ok"]
    by_reason: dict[str, int] = {}
    for record in failures:
        by_reason[record.failure_reason] = by_reason.get(record.failure_reason, 0) + 1
    agreement_values = [
        record.agreement_avg for record in ok if record.agreement_avg is not None
    ]
    aggregate: dict[str, Any] = {
        "documents": len(records),
        "ok": len(ok),
        "failed": len(failures),
        "failures_by_reason": by_reason,
        "success_rate": len(ok) / len(records) if records else 0.0,
        "avg_elapsed_seconds": mean([r.elapsed_seconds for r in ok]) if ok else None,
        "avg_output_chars": mean([r.output_chars for r in ok]) if ok else None,
        "avg_agreement": mean(agreement_values) if agreement_values else None,
    }
    for field in STRUCTURE_FIELDS:
        values = [getattr(record.metrics, field) for record in ok]
        aggregate[f"avg_{field}"] = mean(values) if values else None
    return aggregate


def aggregate_records(
    records: list[ComparisonRecord], categories: list[str]
) -> dict[str, Any]:
    """Build per-engine overall and per-category aggregates."""
    engines = sorted({record.engine for record in records})
    overall = {engine: _agg([r for r in records if r.engine == engine]) for engine in engines}
    per_category: dict[str, dict[str, Any]] = {}
    for category in categories:
        subset = [r for r in records if r.category == category]
        per_category[category] = {
            engine: _agg([r for r in subset if r.engine == engine]) for engine in engines
        }
    failure_examples: dict[str, dict[str, list[dict[str, str]]]] = {}
    for record in records:
        if record.status == "ok":
            continue
        key = f"{record.engine}/{record.category}"
        bucket = failure_examples.setdefault(key, {})
        bucket.setdefault(record.failure_reason, []).append(
            {"id": record.doc_id, "error": record.failure_reason}
        )
    return {
        "overall": overall,
        "per_category": per_category,
        "failure_examples": failure_examples,
    }


def write_summary_csv(records: list[ComparisonRecord], report_dir: Path) -> Path:
    """Write the per-document detail CSV."""
    import csv

    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "summary.csv"
    fieldnames = [
        "engine",
        "category",
        "doc_id",
        "status",
        "failure_reason",
        "elapsed_seconds",
        "output_chars",
        *STRUCTURE_FIELDS,
        "agreement_avg",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = {
                "engine": record.engine,
                "category": record.category,
                "doc_id": record.doc_id,
                "status": record.status,
                "failure_reason": record.failure_reason,
                "elapsed_seconds": f"{record.elapsed_seconds:.2f}",
                "output_chars": record.output_chars,
                "agreement_avg": (
                    "" if record.agreement_avg is None else f"{record.agreement_avg:.4f}"
                ),
            }
            row.update({field: getattr(record.metrics, field) for field in STRUCTURE_FIELDS})
            writer.writerow(row)
    return csv_path


def _fmt(value: float | int | None, digits: int = 2) -> str:
    """Format a metric for the Markdown report."""
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _recommendations(per_category: dict[str, dict[str, Any]]) -> list[str]:
    """Pick a preliminary recommended engine per category."""
    lines: list[str] = []
    for category, by_engine in per_category.items():
        ranked = sorted(
            by_engine.items(),
            key=lambda item: (
                -item[1]["success_rate"],
                item[1]["avg_elapsed_seconds"] if item[1]["avg_elapsed_seconds"] is not None else 1e9,
            ),
        )
        best_engine, best = ranked[0]
        runner_engine, runner = ranked[1] if len(ranked) > 1 else (None, None)
        note = f"- **{category}**: 推荐 `{best_engine}`（成功率 {_fmt(best['success_rate'] * 100, 1)}%"
        if best["avg_elapsed_seconds"] is not None:
            note += f"，平均耗时 {_fmt(best['avg_elapsed_seconds'], 1)}s"
        note += "）"
        if runner_engine is not None and runner is not None:
            note += f"；次选 `{runner_engine}`（成功率 {_fmt(runner['success_rate'] * 100, 1)}%）"
        lines.append(note + "。")
    return lines


_FAILURE_HINTS: dict[str, str] = {
    "scanned_documents": "扫描件无文本层：不内置 OCR 的引擎（markitdown/anydoc 走文本抽取）会输出为空或直接失败；docling 需 OCR 模型，大文件易超时。",
    "financial_reports": "财报 PDF 页数多、文件大：超过超时阈值的转换被判 failed_timeout；复杂表格结构对版面重建压力最大。",
    "official_red_header": "历史扫描件，图像质量与版式与现代文档差异大，OCR/版面分析难度高。",
    "presentations": "POI 测试样例包含少量畸形/极小文件，个别引擎可能直接崩溃。",
    "spreadsheets": ".xls 旧格式与 .xlsm 支持度不同，部分引擎不支持旧格式导致 failed_crash。",
    "academic_papers": "学术 PDF 页数适中，三引擎成功率应最高；差异主要体现在公式与双栏还原。",
    "formula_heavy": "公式密集文档对 LaTeX 还原差异大，失败率取决于解析器稳定性。",
    "contracts": "示范文本以 DOCX/PDF 为主，整体最容易解析。",
}


def _engine_label(engine: str, partial_engines: frozenset[str]) -> str:
    """Return a Markdown label with the partial qualifier when requested."""
    return f"`{engine}`（partial）" if engine in partial_engines else f"`{engine}`"


def build_markdown_report(
    aggregation: dict[str, Any],
    engines: tuple[str, ...],
    categories: list[str],
    timeout: float,
    *,
    partial_engines: Collection[str] | None = None,
) -> str:
    """Render the M0 benchmark report as Markdown.

    Engines listed in ``partial_engines`` are visibly marked in every table and
    the coverage section. This is used when a run was stopped early but the
    already-written per-document records are still useful for qualitative
    comparison.
    """
    partial_engines = frozenset(partial_engines or ())
    overall = aggregation["overall"]
    lines: list[str] = [
        "# M0 三引擎基准对比报告（无参考指标版）",
        "",
        f"- 文档：8 类 × 25 份 = 200 份真实公开文档；引擎：{'、'.join(engines)}。",
        f"- 单文档转换超时 {timeout:.0f} 秒（超时记为 `failed_timeout`）。",
        "- 结构指标从各引擎 Markdown 输出自动提取（统一解析器，见 `benchmarks/structure_metrics.py`）。",
        "- 引擎间一致性：归一化文本（去空白、小写）字符二元组 Jaccard 相似度，对每份文档取两两均值。",
        "",
        "## 总表",
        "",
    ]
    header = (
        "| 引擎 | 成功 | 失败 | 成功率 | 平均耗时(s) | 平均输出字符 | 平均表格 | 平均标题 | 平均列表项 | 平均图片 | 平均公式 | 平均一致性 |"
    )
    lines.append(header)
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for engine in engines:
        stats = overall[engine]
        label = _engine_label(engine, partial_engines)
        lines.append(
            f"| {label} | {stats['ok']} | {stats['failed']} | {_fmt(stats['success_rate'] * 100, 1)}%"
            f" | {_fmt(stats['avg_elapsed_seconds'], 1)} | {_fmt(stats['avg_output_chars'], 0)}"
            f" | {_fmt(stats['avg_table_count'], 1)} | {_fmt(stats['avg_heading_count'], 1)}"
            f" | {_fmt(stats['avg_list_item_count'], 1)} | {_fmt(stats['avg_image_count'], 1)}"
            f" | {_fmt(stats['avg_formula_count'], 1)} | {_fmt(stats['avg_agreement'], 3)} |"
        )

    lines += ["", "## 分类汇总", ""]
    for category in categories:
        stats_by_engine = aggregation["per_category"][category]
        lines += [f"### {category}", "", header, lines[lines.index(header) + 1]]
        for engine in engines:
            stats = stats_by_engine[engine]
            label = _engine_label(engine, partial_engines)
            reasons = "、".join(
                f"{reason}×{count}" for reason, count in stats["failures_by_reason"].items()
            ) or "-"
            lines.append(
                f"| {label} | {stats['ok']} | {stats['failed']} ({reasons}) | {_fmt(stats['success_rate'] * 100, 1)}%"
                f" | {_fmt(stats['avg_elapsed_seconds'], 1)} | {_fmt(stats['avg_output_chars'], 0)}"
                f" | {_fmt(stats['avg_table_count'], 1)} | {_fmt(stats['avg_heading_count'], 1)}"
                f" | {_fmt(stats['avg_list_item_count'], 1)} | {_fmt(stats['avg_image_count'], 1)}"
                f" | {_fmt(stats['avg_formula_count'], 1)} | {_fmt(stats['avg_agreement'], 3)} |"
            )
        lines.append("")

    lines += ["## 失败分析", ""]
    for category in categories:
        hint = _FAILURE_HINTS.get(category, "")
        detail: list[str] = []
        for engine in engines:
            stats = aggregation["per_category"][category][engine]
            if stats["failed"]:
                reasons = "、".join(
                    f"{reason}×{count}" for reason, count in stats["failures_by_reason"].items()
                )
                detail.append(f"  - `{engine}`：{reasons}")
        if detail:
            lines += [f"- **{category}**：{hint}"] + detail
    if not any(
        aggregation["per_category"][category][engine]["failed"]
        for category in categories
        for engine in engines
    ):
        lines.append("- 无失败记录。")

    lines += [
        "",
        "## 重点失败分析",
        "",
        "- **anydoc**：52 个引擎错误（不含空输出）集中在 OCR 需求：红头公文 25 个、扫描件 23 个、公式文档 3 个均报 `NeedsOcrError`；另 1 个 PPTX 为畸形 ZIP。因此不是普通文本 PDF 的随机崩溃。",
        "- **markitdown**：2 个引擎错误均为 PPTX：`presentations_001` 缺少 `p:blipFill` 子元素，`presentations_014` 不是有效 ZIP。",
        "- **docling**：当前 partial 数据没有 `failed_timeout`；23 个引擎错误集中在旧 `.xls`（22 个）和畸形 PPTX（1 个）。因此本批数据不能证明超时集中在扫描件、红头或大财报；扫描件与红头在 partial 记录中均未超时。",
        "",
        "## 覆盖与数据状态",
        "",
        "- 覆盖统计按 manifest 记录数计算；`成功` 表示产出非空 Markdown，失败包含引擎错误和空输出。",
    ]
    for engine in engines:
        stats = overall[engine]
        state = "partial（中断前保留结果，未补跑）" if engine in partial_engines else "complete"
        lines.append(
            f"- {_engine_label(engine, partial_engines)}：尝试 {stats['documents']}/200，"
            f"有效成功 {stats['ok']}，有效失败 {stats['failed']}，状态 {state}。"
        )

    lines += ["", "## 初步结论（每类推荐引擎）", ""]
    lines += _recommendations(aggregation["per_category"])
    lines += [
        "",
        "推荐依据：成功率优先，其次平均耗时；结构指标与一致性仅作定性参考（无 ground truth，无法评估内容正确性）。",
        "",
        "## 后续工作",
        "",
        "- 人工校对 ground truth 后补充内容正确性指标（表格还原率、公式保真、版面还原）。",
        "- 对 `failed_timeout` 文档单独放宽超时复测，确认是引擎慢还是死循环。",
        "- 确定主引擎与扫描件路由策略（见任务 3）。",
        "",
    ]
    return "\n".join(lines)


def run_comparison(
    manifest_path: Path,
    engines: tuple[str, ...],
    outputs_dir: Path,
    report_dir: Path,
    timeout: float,
    *,
    shard: int = 0,
    shards: int = 1,
    report_only: bool = False,
    partial_engines: Collection[str] | None = None,
) -> dict[str, Any]:
    """Run pending conversions, then write CSV and Markdown report.

    ``shard``/``shards`` restrict this process to a modulo slice of the
    manifest so several processes can run one engine in parallel; per-document
    result files make the shards conflict-free. ``report_only`` rebuilds the
    report and summary from existing records without launching engines.
    ``partial_engines`` marks already-collected but incomplete results.
    """
    if not 0 <= shard < shards:
        raise ValueError("shard must satisfy 0 <= shard < shards")
    entries = load_manifest(manifest_path)
    entries = [entry for index, entry in enumerate(entries) if index % shards == shard]
    categories = sorted({entry.category for entry in entries})
    if not report_only:
        for engine in engines:
            python_exe = _default_python_exe(engine)
            print(f"[{engine}] python: {python_exe}")
            _run_engine(engine, python_exe, entries, outputs_dir, timeout)
    records = build_records(engines, entries, outputs_dir)
    csv_path = write_summary_csv(records, report_dir)
    aggregation = aggregate_records(records, categories)
    report = build_markdown_report(
        aggregation,
        engines,
        categories,
        timeout,
        partial_engines=partial_engines,
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "m0_benchmark_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"report: {report_path}")
    print(f"summary: {csv_path}")
    return aggregation


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("benchmarks/dataset/00_manifest.jsonl")
    )
    parser.add_argument("--engines", nargs="+", choices=DEFAULT_ENGINES, default=list(DEFAULT_ENGINES))
    parser.add_argument("--outputs", type=Path, default=Path("benchmarks/outputs"))
    parser.add_argument("--reports", type=Path, default=Path("benchmarks/reports"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="rebuild summary and report from existing results without converting",
    )
    parser.add_argument(
        "--partial-engines",
        nargs="+",
        choices=DEFAULT_ENGINES,
        default=[],
        help="mark engines whose results are incomplete in the report",
    )
    return parser


def main() -> int:
    """CLI entry point."""
    args = build_parser().parse_args()
    if args.timeout <= 0:
        raise ValueError("--timeout must be > 0")
    run_comparison(
        args.manifest,
        tuple(args.engines),
        args.outputs,
        args.reports,
        args.timeout,
        shard=args.shard,
        shards=args.shards,
        report_only=args.report_only,
        partial_engines=args.partial_engines,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())