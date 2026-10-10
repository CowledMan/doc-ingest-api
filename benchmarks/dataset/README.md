# 中文文档基准测试集

本目录保存基准集 manifest 与评测输入。当前为 **8 类 × 25 份 = 200 份真实公开文档**。真实文件保存在本地（git-ignored），manifest 只记录元数据（来源 URL、SHA256、下载日期），可以入库。

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

`benchmarks/outputs/` 保存三引擎输出、逐文档结果和报告；`benchmarks/reports/` 中需要入库的报告单独提交。

## 收集原则

1. **只收公开来源**：政府主动公开文件、arXiv/ACL 等允许研究使用的论文、公开披露文件、公有领域扫描件、Apache/MIT 测试样例。
2. **记录到文件级来源**：`source_url` 保存能直接打开/下载该文件的 URL 或来源页 URL；不要只记录一个搜索页。
3. **本地评测优先**：如果来源允许下载和研究但不允许再分发，文件只放在 `inputs/`，manifest 只保存 URL 和哈希。
4. **先脱敏，后入库 manifest**：对包含个人信息、签名、账号、住址的公开样本，先在本地制作脱敏版；`sha256` 记录的是脱敏后文件的哈希。
5. **保持解析难度**：不要把 PDF 转成 Word 再收集；不要压缩成图片再转 PDF，除非测试的是扫描件；不要把表格截图成纯文本。
6. **命名与配额**：每个已知占位 id 放一份文件，如 `inputs/contracts/contracts_001.docx`；目标仍是每类 25 份。重复版式最多保留 2-3 份，优先补不同行业、版式、表格复杂度的样本。

## 每类公开样本指引

| 类别 | 首选来源 | 查找方式 | 许可/使用注意 |
|---|---|---|---|
| `contracts` | 市场监管总局“合同示范文本库”；省/市市场监管局“合同示范文本”栏目 | 搜“合同示范文本 PDF”，选房屋租赁、买卖、装修、旅游、家政、劳务、运输等不同类型 | 官方主动公开的示范文本；避免收录当事人已签署的真实合同 |
| `academic_papers` | arXiv、ACL Anthology、开放获取期刊/机构知识库 | 按“中文”“Chinese”、NER、表格抽取、文档理解检索 PDF | 遵守论文许可；只用可公开下载版本，不用知网等受权限制的全文 |
| `financial_reports` | 巨潮资讯网、上交所/深交所、港交所披露易、SEC EDGAR | 搜上市公司年度报告、半年度报告、招股说明书；优先大表格和多页附注 | 公开披露文件可用于本地评测；不要再分发原文，也不要用客户未公开材料 |
| `scanned_documents` | Internet Archive、HathiTrust 全文可读项、公共图书馆数字古籍 | 搜中文书刊/报纸扫描 PDF；优先 1929 年前出版、Public Domain 标记 | 公有领域扫描件较安全；避开来源声明禁止批量下载的项目 |
| `presentations` | Apache POI/LibreOffice 测试样例库；政府/高校网站公开 PPT 附件 | 找 `.pptx` 测试文件；补中文公开课件、政策解读、年报路演材料 | Apache/MIT 许可样例可直接用；网站附件按页面声明判断 |
| `spreadsheets` | Apache POI/LibreOffice 测试样例库；政府开放数据平台的原始 `.xlsx` | 找含多 sheet、合并单元格、日期、公式、中文表头的文件 | Apache/MIT 样例安全；开放数据通常允许复用，仍保存来源 URL |
| `official_red_header` | Internet Archive 政府公报/历史公文；政府网站公开公文附件 | 搜“公报 PDF”“政府公报”“红头文件 公开” | 只用主动公开文件；历史公报优先公有领域 |
| `formula_heavy` | arXiv `math.*`、`cs.CL`、物理/统计论文；LaTeX 生成的开放教材 | 搜 “Chinese math paper PDF”“LaTeX formula PDF” | arXiv 分发许可适合研究；避免只抓公式截图，保留原生 PDF |

补充建议：

- 如果某类难以找到现代中文样本，可先用公开测试样例占位，再逐步替换为更贴近产品的真实样本；manifest `features` 说明其代表的能力。
- 对扫描件，优先选择包含表格、印章、竖排/横排、繁简混排和不同字号的样本。
- 对财报，优先收入附注、关联方披露、现金流量表、跨页大表；不要只收封面和目录。
- 对红头公文，保留标题、文号、主送机关、正文、落款和印章区域；不要人工裁掉红头。

## 脱敏清单

按文档类型检查：

- **合同**：当事人姓名/公司名改为“甲方”“乙方”或 `张三/李四/某某公司`；统一社会信用代码、身份证号、手机号、邮箱、银行账号改为同位数假数据；地址保留省市/区县即可；签字和手写签名移除或替换为“签名”。
- **财报**：只用公开披露文件。若补内部样例，必须另建本地数据集，不进本基准。
- **论文/PPT/表格**：作者和机构本来就是公开信息时可保留；未发表草稿、联系方式、学生成绩、员工信息要打码。
- **扫描件/红头公文**：历史文献通常无需脱敏；若出现现代个人信息、收件人手机号、身份证号，用同底色矩形遮盖，避免破坏整页版式。
- **开放数据表**：保留列名、表头、合并单元格、公式；若包含具体自然人或敏感主体，替换成编号，不要删整列导致表格结构改变。
- **公式**：不要用截图替换公式；脱敏公式时保持 LaTeX/Word 公式排版。
- **印章**：公司真实印章建议模糊或用“示例章”替换；政府历史印章一般保留，因为它是版面特征。

脱敏操作建议：

1. 复制原文件到工作目录，不要在唯一副本上修改。
2. 用 PDF 编辑器/Office 修订功能改文字或添加遮盖框；扫描件用图像遮盖。
3. 目视检查每一页，另检查：姓名、电话、证件号、邮箱、账号、地址、签名、二维码、水印。
4. 用新文件名保存，例如 `contracts_001.pdf`，再把脱敏后文件放入 `inputs/<category>/`。
5. `sha256` 只记脱敏后文件；原始文件和脱敏工作底稿不要放入仓库。

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
  "source_url": "https://example.gov.cn/file.pdf",
  "sha256": "…",
  "downloaded_at": "2026-10-10"
}
```

`input`、`ground_truth` 相对于 manifest 所在目录；`status` 由导入/下载脚本维护（`pending_source_document` → `ready`）。

## 导入真实文档

文件放入 `benchmarks/dataset/inputs/<category>/` 后先干跑：

```powershell
uv run python -m benchmarks.import_documents --dry-run
```

需要登记新文件名（不是已有占位 id）时加 `--allow-new`：

```powershell
uv run python -m benchmarks.import_documents --allow-new
```

导入脚本会：

- 按 `(category, 文件名 stem)` 匹配已有占位行；
- 更新实际 `input` 后缀；
- 计算/刷新 `sha256`；
- 新增行自动填 `title`、类别标签、features 和今天日期；
- 把缺失来源设为 `source_url: "unknown"`，并在 `unknown_source` 中汇总。

随后人工回填 `source_url` 和更可读的 `title`，再校验：

```powershell
uv run python -m benchmarks.validate_manifest --manifest benchmarks/dataset/00_manifest.jsonl --min-count 20
```

检查项：manifest 字段完整、文件存在、SHA256 一致、id/哈希无重复、每类数量 ≥ `--min-count`（默认 20，即 80% 达标线）。退出码非 0 表示校验失败。

## 下载与再生成

```powershell
# 按需重下/补齐某几类（已存在且哈希一致的文件自动跳过）
uv run python -m benchmarks.download_dataset --quota 25 --categories contracts scanned_documents
```

自动下载脚本已覆盖一部分公开来源；手动补样本时优先按上面的文件级来源登记。

## 评分维度

- **表格还原率**：以人工校对 Markdown 表格为基准，评估行和单元格召回。
- **公式保真度**：抽取 `$...$` / `$$...$$`，评估公式召回和精度。
- **版面/阅读顺序**：以非空行和标题序列对比阅读顺序。
- **耗时/失败率**：由 runner 记录。
- 复杂印章、手写批注、红头效果仍需人工评分；这些维度后续可在 manifest `features` 中扩展。

## 隐私与版权红线

- 禁止使用含个人隐私、客户数据或版权不明的文档；无 License 的仓库模板一律不用。
- 真实文件只留本地 `inputs/`（已忽略），manifest 只记录 URL + 哈希 + 日期。
- 不要把 `sources.csv`、下载日志、原文件名快照或脱敏工作目录提交进仓库。
- 引入新来源前先确认页面许可；必要时在 commit/PR 描述中记录许可依据。
