# Python SDK

The `doc-ingest-sdk` package provides a typed client for the Document Ingest API.

## Installation

The SDK is included as a workspace dependency via `uv sync`.

## Usage

### Synchronous parse

```python
from doc_ingest_sdk import DocIngestClient

client = DocIngestClient(base_url="http://localhost:8000", api_key="dip_...")
result = client.parse_document(file_path="report.pdf", category="financial_reports")
print(result.markdown)
print(result.metadata["engine_used"])
```

### Asynchronous parse

```python
job = client.queue_parse_document(file_path="report.pdf")
print(job.job_id, job.status)

status = client.check_job(job_id=job.job_id)
if status.status == "succeeded":
    print(status.result.markdown)
```

### Billing (mock)

```python
sub = client.get_subscription()
print(sub.status, sub.plan_name)
```

## Error handling

```python
from doc_ingest_sdk import DocIngestError, QuotaExceededError

try:
    result = client.parse_document(file_path="large.pdf")
except QuotaExceededError:
    print("Upgrade your plan")
except DocIngestError as e:
    print(f"API error: {e}")
```
