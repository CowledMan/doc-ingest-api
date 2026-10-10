# AGENTS.md

本仓库当前处于 **M1：API 网关与引擎接入层**。M0 引擎验证已关闭；评测代码只作为回归参考，不再扩展。

## 1. 阶段边界

- M1 当前批次只做引擎接入层、FastAPI 网关、鉴权/配额、异步任务抽象和 CI。
- 不实现真实 Stripe 支付、订阅、发票或客户管理；Stripe 只保留空配置与 mock/stub。
- OCR 策略仍采用 **按量付费的第三方云 OCR（阿里云）**；当前批次只做 OCR stub，不实现 provider 代码。
- 不要把内部业务决策、客户文档或真实密钥写入提交。
- 产品以海外市场优先；不要添加 ICP 备案、微信/支付宝等国内收款或国内部署配置。
- M0 的 `benchmarks/` 继续保持评测用途；不要把评测专用代码迁入产品运行链路。

## 2. 目标技术栈

- Python 3.12，依赖用 uv 管理，`pyproject.toml` 是唯一依赖入口。
- 服务层：FastAPI；异步任务：Celery/Redis；存储：Postgres。
- 引擎接入对象：anydoc、markitdown、docling；三者均为 MIT。
- 扫描件/复杂版面：按类别路由到云 OCR；云 OCR 只通过环境变量配置密钥。

## 3. 代码规范

- 所有新增 Python 代码必须有类型标注，公共 API 需要 docstring。
- 优先使用 `pathlib.Path`、`dataclasses`、`enum.StrEnum`、标准库。
- 提交信息使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`。
- 单一逻辑变更一次提交；不要混合业务代码与 vendor 修改。
- 每个可执行脚本必须能独立运行并在失败时返回非零退出码。
- 新增或修改可测试逻辑时同步新增 pytest 用例。
- 常用命令：
  - `uv sync`
  - `uv run pytest`
  - `uv run ruff check .`
  - `uv run mypy src benchmarks scripts`

## 4. 协议红线

- **禁止引入 GPL、AGPL 或其他具有强传染性/不明确商用条款的依赖**，包括作为生产依赖、构建依赖或服务集成。
- 生产链路只允许进入协议明确、可闭源商用的代码；当前核心候选只允许 MIT 系依赖。
- MinerU 或其他 AGPL 系项目不得出现在生产依赖、镜像或直接复制的源码中；只允许独立隔离评估结论。
- 引入任何第三方依赖前必须确认 LICENSE，并把风险记录到 PR/commit 描述。
- `vendor/` 内的上游仓库只做本地评估：不要修改 vendor 源码，不要删除或改写上游 LICENSE，不要把 vendor 仓库提交进本仓库。
- `.env`、真实密钥、客户文档、内部定价数据不得入库；示例配置只能使用空值或占位符。

## 5. 评测与数据

- 基准目录由 `benchmarks/` 维护；真实中文样本放在本地数据目录，不得提交含客户或受版权保护内容。
- 评分至少覆盖：表格还原率、公式保真、版面/阅读顺序还原，另记录耗时与失败率。
- 输出结果只写入 `benchmarks/outputs/`、`benchmarks/results/` 或本地忽略目录。
- 修改评测指标时必须保持结果文件向后兼容或在 commit 中说明迁移方式。
