# 中文文档基准测试集

本目录保存基准集 manifest 与评测输出。当前为 **8 类 × 25 份 = 200 份真实公开文档**。真实文件保存在本地（git-ignored），manifest 只记录元数据（来源 URL、SHA256、下载日期），可以入库。

## 目录结构

```text
benchmarks/dataset/
├── 00_manifest.jsonl      # 200 行元数据（入库）
├── inputs/                # 真实文档（git-ignored，只留本地）
│   ├── contracts/
│   ├── academic_papers/
│   ├── financial_reports/
│   ├── scanned_documents/
│   ├── presentations/
│   ├── spreadsheets/
│   ├── official_red_header/
│   └── formula_heavy/
├── ground_truth/          # 人工校对 Markdown（git-ignored，待补齐）
└── README.md
```

`benchmarks/results/` 不入库，保存三引擎输出、逐文档分数和 summary。

## 类别与实际来源（2026-10-10 下载）

| 类别 | 份数 | 来源 | 许可依据 |
|---|---:|---|---|
| `contracts` | 25 | 市场监管总局合同示范文本库（htsfwb.samr.gov.cn）、辽宁/临夏/山东市场监管局的示范文本栏目 | 政府主动公开的示范文本，官方供公众使用 |
| `academic_papers` | 25 | arXiv `cs.CL` 论文 PDF | arXiv 分发许可（作者保留版权，允许下载与共研） |
| `financial_reports` | 25 | 巨潮资讯网（cninfo.com.cn）深市上市公司定期报告 | 公开披露文件；仅限本地评测，不得再分发原文 |
| `scanned_documents` | 25 | Internet Archive 1800-1929 年中文报刊/书籍扫描 PDF | 公有领域（1929 年前出版） |
| `presentations` | 25 | Apache POI 测试样例库 `test-data/slideshow/*.pptx` | Apache-2.0 |
| `spreadsheets` | 25 | Apache POI 测试样例库 `test-data/spreadsheet/*.xls(x|m)` | Apache-2.0 |
| `official_red_header` | 25 | Internet Archive《国民政府公报》（1929-1948）扫描件，含红头与印章 | 公有领域 |
| `formula_heavy` | 25 | arXiv `math.AP` 论文 PDF（公式密集） | arXiv 分发许可 |

说明：

- `academic_papers` 与 `formula_heavy` 以英文论文为主（M0 阶段先验证解析能力，中文论文样本后续人工补充）；
- 扫描件与红头公文为历史文献扫描，版面/印章特征真实，但字号与现代公文有差异；
- 每份文件的准确来源 URL 记录在 manifest 的 `source_url` 字段。

## manifest 格式

每行一个 JSON 对象：

```json
{
  "id": "contracts_001",
  "category": "contracts",
  "category_label": "中文合同",
  "input": "inputs/contracts/contracts_001.pdf",
  "ground_truth": "ground_truth/contracts/contracts_001.md",
  "features": ["tables", "chinese_text", "layout"],
  "language": "zh-Hans",
  "status": "ready",
  "title": "旅游合同（示范文本）",
  "source_url": "https://scjg.linxia.gov.cn/api-gateway/...",
  "sha256": "…",
  "downloaded_at": "2026-10-10"
}
```

`input`、`ground_truth` 相对于 manifest 所在目录；`status` 由下载/导入脚本维护（`pending_source_document` → `ready`）。

## 校验

```powershell
uv run python -m benchmarks.validate_manifest --manifest benchmarks/dataset/00_manifest.jsonl --min-count 20
```

检查项：manifest 字段完整、文件存在、SHA256 一致、id/哈希无重复、每类数量 ≥ `--min-count`（默认 20，即 80% 达标线）。退出码非 0 表示校验失败。

## 再生成与补充

```powershell
# 按需重下/补齐某几类（已存在且哈希一致的文件自动跳过）
uv run python -m benchmarks.download_dataset --quota 25 --categories contracts scanned_documents
```

手动补充文档时：按 `inputs/<category>/<id><扩展名>` 命名放入后运行：

```powershell
uv run python -m benchmarks.import_documents --manifest benchmarks/dataset/00_manifest.jsonl --dry-run
```

`import_documents` 会补 `source_url` 为 `unknown`，请人工回填后重跑校验。

## 评分维度

- **表格还原率**：以人工校对 Markdown 表格为基准，评估行和单元格召回。
- **公式保真度**：抽取 `$...$` / `$$...$$`，评估公式召回和精度。
- **版面/阅读顺序**：以非空行和标题序列对比阅读顺序。
- **耗时/失败率**：由 runner 记录。
- 复杂印章、手写批注、红头效果仍需人工评分；这些维度后续可在 manifest `features` 中扩展。

## 隐私与版权红线（后续补样本时继续遵守）

- 只使用允许公开分发的样本；政府公开文件、arXiv、公开披露文件、公有领域扫描、Apache-2.0 测试样例。
- 禁止使用含个人隐私、客户数据或版权不明的文档（无 License 的仓库模板一律不用）。
- 人名 → `张三`/`李四` 占位；证件号、手机号、银行卡号 → 同位数假数据；详细住址打码；签名/印章 → 移除或假名。
- 脱敏保持字体、对齐、表格结构、公式排版不变，否则版面评分失真。
- 真实文件只留本地 `inputs/`（已忽略），manifest 只记录 URL + 哈希 + 日期。