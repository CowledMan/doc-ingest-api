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

`benchmarks/results/` 不入库，保存三引擎输出、逐文档分数和 summary。`inputs/`、`ground_truth/` 与本地来源记录均已加入 `.gitignore`，真实文件只留本地。

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

## 文件命名规范

- 输入文件放入 `inputs/<category>/`，文件名为 `<id><扩展名>`，例如 `inputs/contracts/contracts_001.pdf`。
- 人工校对的 ground truth 放入 `ground_truth/<category>/<id>.md`，例如 `ground_truth/contracts/contracts_001.md`。
- 同一个 id 只能有一个输入文件；同名不同扩展名会被导入脚本判为冲突。
- 扩展名可以和占位记录不同（如占位 `.pdf` 实际放 `.docx`），导入脚本会把 `input` 路径更新为实际文件。

## 文档收集指引

原则：优先使用**自制样本**或**政府/官方主动公开**的文件；每个类别先收集 3-5 份跑通流程，再逐步补齐到 25 份。

| 类别 | 推荐来源 | 版权与使用注意 |
|---|---|---|
| `contracts` | ① 自拟合同；② 市场监管总局“合同示范文本库”与各地政府发布的示范文本 | 自拟最稳妥；示范文本按官方说明使用；禁止使用真实客户合同 |
| `academic_papers` | ① 自写小样；② 高校机构知识库中开放获取的学位论文；③ 公开课程讲义 | 多数期刊论文受版权保护，仅限本地评测，不得再分发 |
| `financial_reports` | 巨潮资讯网（cninfo.com.cn）、港交所披露易的上市公司定期报告 | 属公开披露文件，可下载做本地评测；记录来源 URL 与下载日期；不得再分发原文 |
| `scanned_documents` | ① 自己打印文档后扫描（可控性最好）；② 国家图书馆“中华古籍资源库”、Internet Archive 中允许再利用的中文扫描书 | 自制样本可覆盖倾斜、噪点等真实场景；古籍与扫描书遵循站点使用条款 |
| `presentations` | ① 自制中文 PPT；② 公开会议/政府宣讲 slides（确认许可） | 自制最省事；引用他人 slides 前确认授权 |
| `spreadsheets` | ① 国家统计局、各地公共数据开放平台的开放数据表；② 自建含合并单元格、多级表头的样本 | 优先 CC0/开放许可数据；自建可精确覆盖复杂表头场景 |
| `official_red_header` | 中国政府网政策文件库（www.gov.cn）、各级政府信息公开平台的主动公开文件 | 只使用政府主动公开的文件；发文机关名称保留真实值（公开信息） |
| `formula_heavy` | ① 自编 LaTeX 导出 PDF（可控公式密度与排版）；② 开放课程讲义 | 自编可控性最好；确认讲义许可后再使用 |

### 脱敏规则

隐私红线——下列信息进入本地数据目录前必须替换：

| 敏感信息 | 替换方式 |
|---|---|
| 人名 | `张三`、`李四` 等占位名 |
| 公司/机构名 | 虚构名，如 `示例科技有限公司` |
| 身份证号、手机号、银行卡号 | 同位数假数据（假号段/占位数字），保持原格式与长度 |
| 邮箱、网址中的用户标识 | `user@example.com` 类占位 |
| 详细住址 | 保留到省/市/区，门牌号打码 |
| 签名、印章文字 | 移除或替换为假名 |

脱敏操作要求：

1. **保持版面不变**：字体、字号、对齐、表格结构、公式排版尽量不要动，否则版面与 OCR 评分会失真。
2. **扫描件**：建议直接打印“假数据版”文档再扫描，比图像修图更可控。
3. **PDF 文本层**：替换文字后确认文档仍可正常抽取文本。
4. **公开财报**：属公开信息无需脱敏，但仍不得把原文再分发或提交入库。

### 版权红线

- 所有真实样本只保存在本地 `benchmarks/dataset/inputs/`（已忽略，不入库）。
- 不要用爬虫批量下载；手动收集少量评测样本即可。
- 建议在本地维护 `benchmarks/dataset/sources.csv`（已忽略）记录：文件名、来源 URL、下载日期、许可说明。

## 导入真实文档

收集完成后，把文件按命名规范放入 `inputs/<category>/`，然后注册进 manifest：

```powershell
# 预检：只报告将要注册的内容，不写入 manifest
uv run python -m benchmarks.import_documents --manifest benchmarks/dataset/00_manifest.jsonl --dry-run

# 写入：匹配行的 status 置为 ready
uv run python -m benchmarks.import_documents --manifest benchmarks/dataset/00_manifest.jsonl
```

导入规则：

- 按 `inputs/<category>/<文件名 stem>` 匹配 manifest 行的 `category` + `id`，匹配后把 `input` 更新为实际文件路径、`status` 置为 `ready`。
- 文件 stem 没有对应占位行时默认报错；确认要新增记录时加 `--allow-new`（`id` 取文件名 stem，`category` 取所在目录名，目录必须是已知类别）。
- 退出码非 0 表示有文件未匹配、stem 冲突或 manifest 格式有误。

### 状态流转

```text
pending_source_document（占位） → 导入脚本置 ready → 补齐 ground truth → run_benchmark
```

说明：`run_benchmark` 只检查 `input` 与 `ground_truth` 文件是否存在，不读取 `status` 字段；缺任一文件时该文档记为失败。`status` 仅用于收集进度管理。

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

`input` 和 `ground_truth` 相对于 manifest 所在目录。

## 评分维度

- **表格还原率**：以人工校对 Markdown 表格为基准，评估行和单元格召回。
- **公式保真度**：抽取 `$...$` / `$$...$$`，评估公式召回和精度。
- **版面/阅读顺序**：以非空行和标题序列对比阅读顺序。
- **耗时/失败率**：由 runner 记录。
- 复杂印章、手写批注、红头效果仍需人工评分；这些维度后续可在 manifest `features` 中扩展。