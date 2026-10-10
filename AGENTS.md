# AGENTS.md

本仓库当前处于 **M0：引擎验证与对比**。请勿在本仓库实现业务 API、鉴权、计费或 Stripe 逻辑；这些属于 M1。

## 1. 阶段边界

- M0 只做三引擎验证、最小调用脚本、基准集脚手架和评测报告素材。
- 不要新增 FastAPI/Celery/Postgres/Redis 运行时代码。
- 不要把内部业务决策、客户文档或真实密钥写入提交。
- OCR 策略按当前决定采用 **按量付费的第三方云 OCR（阿里云）**；不要在此阶段实现或维护自部署 GPU OCR。
- 产品以海外市场优先；不要添加 ICP 备案、微信/支付宝等国内收款或国内部署配置。

## 2. 目标技术栈（后续 M1/M2 记忆点）

- Python 3.12，依赖用 uv 管理，`pyproject.toml` 是唯一依赖入口。
- 服务层目标：FastAPI；异步任务目标：Celery/Redis；存储目标：Postgres。
- 解析引擎评估对象：anydoc、markitdown、docling；三者均为 MIT。
- 扫描件/复杂版面：先做可测试的引擎路由与置信度评估；云 OCR 只通过环境变量配置密钥。

## 3. 代码规范

- 所有新增 Python 代码必须有类型标注，公共 API 需要 docstring。
- 优先使用 `pathlib.Path`、`dataclasses`、`enum.StrEnum`、标准库；评测脚本不强制引入 Web 框架。
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
- 若复用上游少量代码或配置模式，必须先确认协议允许，并保留版权与许可声明。
- `.env`、真实密钥、客户文档、内部定价数据不得入库；示例配置只能使用空值或占位符。

## 5. 评测与数据

- 基准目录由 `benchmarks/` 维护；真实中文样本放在本地数据目录，不得提交含客户或受版权保护内容。
- 评分至少覆盖：表格还原率、公式保真、版面/阅读顺序还原，另记录耗时与失败率。
- 输出结果只写入 `benchmarks/results/` 或本地忽略目录。
- 修改评测指标时必须保持结果文件向后兼容或在 commit 中说明迁移方式。
