# M0 引擎决策

## 结论

选择 **`markitdown` 作为 M1 的主引擎**，但不是唯一引擎。实际执行采用“markitdown 主路径 + anydoc 结构化 PDF 副路径 + 云 OCR/docling 扫描件与复杂版面兜底”。

理由：

- **覆盖最稳**：markitdown 在 200 份样本中只有 2 个真实引擎错误，其余 50 个有效失败主要来自无文本层的扫描件/红头公文空输出。这类文件本来就不应由主引擎处理，而应前置路由到云 OCR。
- **延迟可控**：有效成功样本平均 2.2 秒，明显低于 docling 的 101.5 秒；比 anydoc 略慢，但换来更完整的原生 PDF/Office 覆盖。
- **生态与维护成本**：MIT 许可、实现面小、对 Office 文件支持好，适合作为 M1 的默认通道。
- **不是内容正确性结论**：M0 没有 ground truth，本决策只基于成功率、耗时、输出结构和失败模式，不宣称 markitdown 的表格/公式还原最优。

排除项：

- `anydoc` 不做主引擎：速度和表格密度好，但在非扫描样本中仍有公式/PPT/空输出失败。
- `docling` 不做主引擎：扫描件和红头表现好，但平均耗时高，且旧 `.xls` 失败集中；本批数据还只是 partial。

## 路由策略

按文件类型和预检信号先分流，再按输出置信度兜底：

| 类别 | 首选 | 兜底 | 说明 |
|---|---|---|---|
| `contracts` | `markitdown` | `anydoc` → `docling` | 常规合同用主引擎；表格或版面置信度低时切 anydoc |
| `academic_papers` | `markitdown` | `anydoc` → `docling` | 双栏/复杂版面或公式密集时允许 docling 兜底 |
| `financial_reports` | `anydoc` | `markitdown` → `docling` | 财报优先保表格结构；anydoc 输出更紧凑、表格计数更接近业务预期 |
| `scanned_documents` | **云 OCR** | `docling` | 不让 markitdown/anydoc 处理无文本层扫描件 |
| `presentations` | `markitdown` | `anydoc` → `docling` | markitdown 对 PPTX 支持最好 |
| `spreadsheets` | `markitdown` | `anydoc` | `.xls` 不路由 docling；docling 在本批旧 Excel 上失败集中 |
| `official_red_header` | **云 OCR** | `docling` | markitdown 输出空、anydoc 要求 OCR；云 OCR 后再做红头版面重建 |
| `formula_heavy` | `markitdown` | `anydoc` → `docling` | 主路径稳定；公式置信度低时允许 docling 兜底 |

## 扫描件与云 OCR 接口预留

按当前阶段决定，扫描件/复杂版面采用**按量付费的第三方云 OCR（阿里云）**，不实现自部署 GPU OCR。M0 只预留设计，不在仓库实现业务 API。

建议在 M1 中暴露一个窄接口：

- 输入：本地/对象存储文档句柄、页码范围、文档 MIME、语言偏好、版面类型提示。
- 输出：Markdown/结构化块、每页 OCR 置信度、每页耗时、厂商请求 ID、计费计量信息。
- 配置：只允许通过环境变量注入密钥；示例配置使用空值或占位符。
- 路由：先判断 PDF 是否有文本层、图像覆盖率、页数和大小；命中扫描件条件才调用云 OCR。
- 失败处理：限流、超时、重试和降级到 docling；记录 provider request id，便于审计。
- 边界：不写鉴权、计费、Stripe、客户管理或队列实现；只定义引擎接入所需接口。

## 置信度路由触发条件

M0 没有 ground truth，以下为初始阈值，M1 用人工校对样本校准：

- **硬路由到云 OCR**：
  - PDF 无文本层；
  - 平均每页可抽取字符数过低；
  - 图像覆盖率异常高；
  - 页面被判定为扫描图或照片型 PDF。
- **主引擎降级**：
  - 转换异常或超时；
  - 输出为空；
  - 表格置信度低；
  - 版面/阅读顺序置信度低；
  - 标题层级异常缺失；
  - 公式文档的公式置信度低；
  - 输出长度相对同类文档中位数偏离过大。
- **anydoc 副路径**：
  - markitdown 输出缺少表格但文档明显含表格；
  - 财报、合同等结构化 PDF 的表格置信度低。
- **docling 兜底**：
  - 扫描件/红头云 OCR 不可用或置信度低；
  - 复杂双栏、多栏学术版面；
  - 主路径和副路径都失败。
- **不兜底**：
  - 输出置信度高且结构指标满足预期；
  - 避免为每个文档都跑三引擎，控制成本和延迟。

## M1 引擎接入清单

只列工作项，不在 M0 实现：

- [ ] markitdown 主适配器与 Markdown 输出规范
- [ ] anydoc 结构化 PDF 副适配器
- [ ] docling 扫描件/复杂版面兜底适配器
- [ ] 阿里云 OCR provider 接口与配置占位
- [ ] PDF/Office 预分类器
- [ ] 置信度评分器
- [ ] 路由决策器
- [ ] 输出规范化器
- [ ] 失败分类与重试策略
- [ ] 引擎级超时与资源隔离
- [ ] 基准复测与回归集
- [ ] 观测指标：耗时、失败原因、置信度、路由命中率、OCR 用量
- [ ] 许可审查与依赖锁定
