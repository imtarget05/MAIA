"""Marketing / product data pipeline: collect → store → analyse → answer.

Layering (each layer may only depend on the ones below it):

```
collectors.py   connectors + normalisation + run ledger   (no analytics)
warehouse.py    SQLite schema, idempotent upserts, guarded reads
analytics.py    sentiment / topics / KPI rollups / anomaly detection
nl2sql.py       reviewed question→SQL rules with schema verification
```

Every entry point is offline-capable: fixtures in, warehouse out. Remote
collection is opt-in (``MARKET_HTTP_ENABLED``) so a test run or a demo can never
start scraping a third-party site, and no number ever leaves the process without
saying so.
"""
from __future__ import annotations

from .analytics import (
    classify_sentiment,
    detect_kpi_drop,
    kpi_rollup,
    review_insight,
    topic_buckets,
)
from .collectors import (
    CollectedRecords,
    ConnectorError,
    FileConnector,
    HttpJsonConnector,
    IngestResult,
    ingest_metrics,
    ingest_reviews,
    normalize_metric,
    normalize_review,
)
from .nl2sql import NLToSQLPlan, plan_sql, rule_rules
from .warehouse import MarketWarehouse

__all__ = [
    "CollectedRecords",
    "ConnectorError",
    "FileConnector",
    "HttpJsonConnector",
    "IngestResult",
    "MarketWarehouse",
    "NLToSQLPlan",
    "classify_sentiment",
    "detect_kpi_drop",
    "ingest_metrics",
    "ingest_reviews",
    "kpi_rollup",
    "normalize_metric",
    "normalize_review",
    "plan_sql",
    "review_insight",
    "rule_rules",
    "topic_buckets",
]
