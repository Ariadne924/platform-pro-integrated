"""Persisted snapshots of factor-research experiments."""

from __future__ import annotations

import builtins
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb


class ExperimentStore:
    """Small DuckDB store for immutable factor experiment inputs and outputs."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = duckdb.connect(str(self._path))
        self._connection.execute("SET TimeZone = 'UTC'")
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS factor_experiments (
                experiment_id VARCHAR PRIMARY KEY,
                created_at VARCHAR NOT NULL,
                factor_name VARCHAR NOT NULL,
                request_json JSON NOT NULL,
                result_json JSON NOT NULL
            )
        """)
        self._connection.execute("""
            ALTER TABLE factor_experiments
            ALTER created_at SET DATA TYPE VARCHAR USING created_at::VARCHAR
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS ml_experiments (
                experiment_id VARCHAR PRIMARY KEY,
                signature VARCHAR UNIQUE NOT NULL,
                created_at VARCHAR NOT NULL,
                request_json JSON NOT NULL,
                result_json JSON NOT NULL
            )
        """)

    def save(self, request: dict, result: dict) -> str:
        experiment_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO factor_experiments
                    (experiment_id, created_at, factor_name, request_json, result_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    experiment_id,
                    created_at.isoformat(),
                    request["factor"],
                    json.dumps(request),
                    json.dumps(result),
                ],
            )
        return experiment_id

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT experiment_id, created_at, factor_name, request_json, result_json
                FROM factor_experiments
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [self._summary(*row) for row in rows]

    def save_ml(self, signature: str, request: dict, result: dict) -> str:
        """Persist one immutable ML request/result pair, deduplicated by signature."""
        with self._lock:
            existing = self._connection.execute(
                "SELECT experiment_id FROM ml_experiments WHERE signature = ?",
                [signature],
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            experiment_id = str(uuid.uuid4())
            self._connection.execute(
                """
                INSERT INTO ml_experiments
                    (experiment_id, signature, created_at, request_json, result_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    experiment_id,
                    signature,
                    datetime.now(UTC).isoformat(),
                    json.dumps(request, ensure_ascii=False, allow_nan=False),
                    json.dumps(result, ensure_ascii=False, allow_nan=False),
                ],
            )
        return experiment_id

    def list_ml(self, limit: int = 50) -> builtins.list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT experiment_id, signature, created_at, request_json, result_json
                FROM ml_experiments
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        summaries: list[dict] = []
        for experiment_id, signature, created_at, request_json, result_json in rows:
            request = json.loads(request_json)
            result = json.loads(result_json)
            summaries.append(
                {
                    "experiment_id": experiment_id,
                    "signature": signature,
                    "created_at": created_at,
                    "symbols": request.get("symbols", []),
                    "factors": request.get("factors", []),
                    "models": request.get("models", []),
                    "portfolio_method": request.get("portfolio", {}).get(
                        "method", "equal_weight"
                    ),
                    "score": result.get("score", {}).get("score"),
                    "status": result.get("score", {}).get("status"),
                }
            )
        return summaries

    def get_ml(self, experiment_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT experiment_id, signature, created_at, request_json, result_json
                FROM ml_experiments
                WHERE experiment_id = ?
                """,
                [experiment_id],
            ).fetchone()
        if row is None:
            return None
        return {
            "experiment_id": row[0],
            "signature": row[1],
            "created_at": row[2],
            "request": json.loads(row[3]),
            "result": json.loads(row[4]),
        }

    def get(self, experiment_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT experiment_id, created_at, factor_name, request_json, result_json
                FROM factor_experiments
                WHERE experiment_id = ?
                """,
                [experiment_id],
            ).fetchone()
        if row is None:
            return None
        return self._detail(*row)

    @staticmethod
    def _summary(
        experiment_id: str,
        created_at: str,
        factor_name: str,
        request_json: str,
        result_json: str,
    ) -> dict:
        request = json.loads(request_json)
        result = json.loads(result_json)
        return {
            "experiment_id": experiment_id,
            "created_at": created_at,
            "factor_name": factor_name,
            "symbols": request.get("symbols", []),
            # Field removed from the request model — display the configured default.
            "provider_id": request.get("provider_id", "auto-resolved"),
            "in_sample": result["in_sample"]["ic_stats"],
            "out_of_sample": result["out_of_sample"]["ic_stats"],
        }

    def close(self) -> None:
        """Close the DuckDB connection."""
        with self._lock:
            self._connection.close()

    @staticmethod
    def _detail(
        experiment_id: str,
        created_at: str,
        factor_name: str,
        request_json: str,
        result_json: str,
    ) -> dict:
        return {
            "experiment_id": experiment_id,
            "created_at": created_at,
            "factor_name": factor_name,
            "request": json.loads(request_json),
            **json.loads(result_json),
        }
