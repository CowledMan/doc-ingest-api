# 200 份中文基准测试集

本目录保存基准集 manifest 与评测输出。默认生成 200 条占位记录：8 个类别 × 25 份。真实样本与人工校对结果保留在本地，不提交受版权或隐私限制的文件。

## 目录结构

```text
benchmarks/dataset/
├── 00_manifest.jsonl
├── inputs/
│   ├── contracts/
│   ├── academic_papers/
│   ├── financial_reports/
│   ├── scanned_documents/
│   ├── presentations/
│   ├── spreadsheets/
│   ├── official_red_header/
│   └── formula_heavy/
├── ground_truth/
│   └── <同一批分类目录>/
└── README.md
```

`benchmarks/results/` 不入库，保存三引擎输出、逐文档分数和 summary。

## 类别

| key | 中文含义 | 默认扩展名 | 重点 |
|---|---|---|---|
| `contracts` | 中文合同 | `.pdf` | 表格、中文排版 |
| `academic_papers` | 中文学术论文 | `.pdf` | 双栏、公式、表格 |
| `financial_reports` | 中文财报 | `.pdf` | 跨页表格、复杂表头 |
| `scanned_documents` | 中文扫描件 | `.pdf` | OCR、版面 |
| `presentations` | 中文 PPT | `.pptx` | 标题层级、版面 |
| `spreadsheets` | 中文 Excel | `.xlsx` | 表格还原 |
| `official_red_header` | 红头公文 | `.pdf` | 红头、印章、版面 |
| `formula_heavy` | 公式密集文档 | `.pdf` | LaTeX 公式 |

## 使用

```powershell
uv run python -m benchmarks.scaffold_dataset --output benchmarks/dataset --force
```

manifest 每行一个 JSON 对象：

```json
{
  "id": "contracts_001",
  "category": "contracts",
  "category_label": "中文合同",
  "input": "inputs/contracts/contracts_001.pdf",
  "ground_truth": "ground_truth/contracts/contracts_001.md",
  "features": ["tables", "chinese_text", "layout"],
  "language": "zh-Hans",
  "status": "pending_source_document"
}
```

`input` 和 `ground_truth` 相对于 manifest 所在目录。真实文档准备好后，把 `status` 改成 `ready` 或直接删除该字段。

## 评分维度

- **表格还原率**：以人工校对 Markdown 表格为基准，评估行和单元格召回。
- **公式保真度**：抽取 `$...$` / `$$...$$`，评估公式召回和精度。
- **版面/阅读顺序**：以非空行和标题序列对比阅读顺序。
- **耗时/失败率**：由 runner 记录。
- 复杂印章、手写批注、红头效果仍需人工评分；这些维度后续可在 manifest `features` 中扩展。
