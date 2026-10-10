# 文档摄取 API（M0）

这是“任意文档 → 干净 Markdown”解析 API 的 M0 工程仓库：验证 anydoc、markitdown、docling 三个 MIT 引擎，并为 200 份中文样本基准评测搭建脚手架。业务 API、鉴权、计费与 Stripe 接入在 M1 才开始。

## 已完成范围

- Git 与 Python 3.12 / uv 项目骨架。
- `vendor/` 本地评估克隆：`firecrawl/anydoc`、`microsoft/markitdown`、`docling-project/docling`。
- 三引擎最小调用脚本。
- 200 份中文测试集目录脚手架与三引擎基准评测框架。
- OCR 策略采用按量付费云 OCR（阿里云），不自建 GPU。
- 面向海外优先/Paddle 收款；仓库内不做国内备案或国内收款配置。

## 快速开始

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy src benchmarks scripts
```

三引擎冒烟验证：

```powershell
uv run --with firecrawl-anydoc python scripts/verify_anydoc.py
uv run --with markitdown python scripts/verify_markitdown.py
uv run --with docling python scripts/verify_docling.py
```

首次安装 docling 可能会下载较大依赖；首次解析 PDF 可能会下载模型文件。

## 基准集脚手架

生成 200 条中文测试集 manifest 占位与目录结构：

```powershell
uv run python -m benchmarks.scaffold_dataset --output benchmarks/dataset
```

基准集为 8 类 × 25 份真实公开文档（来源、许可与 manifest 字段见 `benchmarks/dataset/README.md`）。重新下载/补齐与校验：

```powershell
uv run python -m benchmarks.download_dataset --quota 25
uv run python -m benchmarks.validate_manifest --manifest benchmarks/dataset/00_manifest.jsonl --min-count 20
```

manifest（来源 URL、SHA256、下载日期）入库；真实文件与 ground truth 只留本地。人工校对 ground truth 待补齐后即可运行 `run_benchmark`。

执行三引擎评测：

```powershell
uv run python -m benchmarks.run_benchmark --manifest benchmarks/dataset/00_manifest.jsonl --engines anydoc markitdown docling
```

评测框架会输出每个文档的 Markdown、逐项分数与汇总报告，评分维度包括：

- 表格还原率；
- 公式保真度；
- 版面/阅读顺序还原；
- 总耗时与失败率。

## 仓库约定

请阅读 [AGENTS.md](./AGENTS.md)。关键红线：不引入 GPL/AGPL 依赖；不修改或提交 vendor 仓库；保留上游 LICENSE；`.env` 不入库。
