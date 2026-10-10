# 文档摄取 API

这是“任意文档 → 干净 Markdown”解析 API 的工程仓库。M0 三引擎验证已完成，当前处于 **M1：API 网关与引擎接入层**。

## 已完成范围

- M0：anydoc、markitdown、docling 三引擎基准与对比报告。
- M1 批次 1：
  - `POST /v1/parse` 同步解析；
  - `POST /v1/parse/async` 与 `GET /v1/jobs/{job_id}`；
  - `GET /healthz`；
  - `X-API-Key` 鉴权与月度页数配额；
  - markitdown 主路径、anydoc 财报副路径、扫描件/红头 OCR stub；
  - Celery/Redis 抽象接口，CI 可用 BackgroundTasks；
  - `scripts/manage_keys.py`。

## 快速开始

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy src benchmarks scripts
```

创建 API Key：

```powershell
uv run python scripts/manage_keys.py create --name local --monthly-page-quota 500
uv run python scripts/manage_keys.py list
```

启动网关：

```powershell
uv run uvicorn doc_ingest_api.asgi:app
```

异步队列默认使用 `QUEUE_BACKEND=background`，CI 不依赖 Redis。生产可切换：

```env
QUEUE_BACKEND=celery
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/doc_ingest
```

扫描件与红头公文当前会先路由到 OCR stub；未配置 `ALIYUN_OCR_KEY` 时返回 `ocr_not_configured`。Stripe 只保留空配置占位，本批次不实现支付逻辑。

## M0 基准集

M0 评测框架和报告仍在 `benchmarks/` 下，用于回归参考。200 份中文样本的来源、许可与 manifest 说明见 `benchmarks/dataset/README.md`。

## 仓库约定

请阅读 [AGENTS.md](./AGENTS.md)。关键红线：不引入 GPL/AGPL 依赖；不修改或提交 vendor 仓库；保留上游 LICENSE；`.env` 不入库。
