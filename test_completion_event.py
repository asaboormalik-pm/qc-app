#!/usr/bin/env python3
"""Standalone test script for the ERP completion_event endpoint."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests


ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_ENDPOINT_URL = ""


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env file without overriding real env vars."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def resolve_completion_endpoint() -> Tuple[str, float]:
    """Resolve completion_event URL and timeout from ERP_ENDPOINTS_JSON."""
    raw = os.getenv("ERP_ENDPOINTS_JSON", "").strip()
    if not raw:
        return DEFAULT_ENDPOINT_URL, 30.0

    try:
        endpoints = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ERP_ENDPOINTS_JSON is invalid JSON: {exc}") from exc

    if not isinstance(endpoints, dict):
        raise ValueError("ERP_ENDPOINTS_JSON must be a JSON object")

    endpoint = endpoints.get("completion_event")
    if not isinstance(endpoint, dict):
        return DEFAULT_ENDPOINT_URL, 30.0

    url = str(endpoint.get("url") or DEFAULT_ENDPOINT_URL).strip()
    timeout = float(endpoint.get("timeout_seconds") or 30.0)
    return url, timeout


def build_test_payload() -> Dict[str, Any]:
    """Build a realistic payload with current IDs and timestamp."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    batch_id = now.strftime("%Y%m%d%H%M%S")
    invoice_id = os.getenv("TEST_COMPLETION_INVOICE_ID", f"INV-{batch_id}")
    message_id = os.getenv("TEST_COMPLETION_MESSAGE_ID", f"{invoice_id}::completion::{uuid.uuid4().hex[:12]}")

    return {
        "message_id": message_id,
        "invoice_id": invoice_id,
        "client_id": os.getenv("TEST_COMPLETION_CLIENT_ID", "CLNT-001"),
        "timestamp": os.getenv("TEST_COMPLETION_TIMESTAMP", now.isoformat().replace("+00:00", "Z")),
        "operator_id": os.getenv("TEST_COMPLETION_OPERATOR_ID", "OPR-123"),
        "warehouse_id": os.getenv("TEST_COMPLETION_WAREHOUSE_ID", "WH-RYD-01"),
        "boxes": [
            {
                "box_id": f"BOX-{batch_id}-01",
                "sscc": "123456789012345678",
                "box_type": "outbound",
                "dm_code": "DM10001",
                "items": [
                    {
                        "sku_id": "SKU-1001",
                        "dm_code": "DM-111001",
                        "ul_code": "UL-111001",
                        "qty": 1,
                        "status": "happy_path",
                    },
                    {
                        "sku_id": "SKU-1005",
                        "dm_code": "DM-111005",
                        "ul_code": "UL-111001",
                        "qty": 1,
                        "status": "damaged",
                    },
                ],
            },
            {
                "box_id": f"BOX-{batch_id}-02",
                "sscc": "223456789012345678",
                "box_type": "anomalies",
                "dm_code": "DM20001",
                "items": [
                    {
                        "sku_id": "SKU-1002",
                        "dm_code": None,
                        "ul_code": "UL-111001",
                        "qty": 2,
                        "status": "excess",
                    },
                    {
                        "sku_id": "TEMP-SKU-001",
                        "dm_code": None,
                        "ul_code": "UL-111001",
                        "qty": 1,
                        "status": "unknown_excess",
                    },
                ],
            },
        ],
        "shortages": [
            {
                "sku_id": "SKU-1003",
                "dm_code": "DM-110102",
                "expected_qty": 1,
                "received_qty": 0,
            },
            {
                "sku_id": "SKU-1004",
                "dm_code": "DM-110134",
                "expected_qty": 1,
                "received_qty": 0,
            },
        ],
        "summary": {
            "total_expected_items": 20,
            "total_received_items": 18,
            "total_shortages": 2,
            "total_excess": 3,
            "total_unknown_items": 1,
            "total_damaged": 1,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test the ERP completion_event endpoint.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved request and exit without sending it.")
    parser.add_argument("--url", help="Override the completion_event URL.")
    parser.add_argument("--timeout", type=float, help="Override the request timeout in seconds.")
    parser.add_argument("--message-id", help="Override payload.message_id.")
    parser.add_argument("--invoice-id", help="Override payload.invoice_id.")
    parser.add_argument("--timestamp", help="Override payload.timestamp (ISO 8601).")
    return parser.parse_args()


def test_completion_event(
    *,
    dry_run: bool = False,
    url_override: Optional[str] = None,
    timeout_override: Optional[float] = None,
    message_id_override: Optional[str] = None,
    invoice_id_override: Optional[str] = None,
    timestamp_override: Optional[str] = None,
) -> bool:
    """Test the completion_event endpoint."""
    load_env_file(ENV_PATH)
    erp_url, timeout_seconds = resolve_completion_endpoint()
    erp_url = url_override or erp_url
    timeout_seconds = timeout_override or timeout_seconds

    if not erp_url:
        print("ERROR: Configure ERP_ENDPOINTS_JSON.completion_event.url or pass --url.")
        return False

    erp_username = os.getenv("ERP_AUTH_BASIC_USERNAME", "").strip()
    erp_password = os.getenv("ERP_AUTH_BASIC_PASSWORD", "").strip()
    test_payload = build_test_payload()

    if message_id_override:
        test_payload["message_id"] = message_id_override
    if invoice_id_override:
        test_payload["invoice_id"] = invoice_id_override
    if timestamp_override:
        test_payload["timestamp"] = timestamp_override

    print("=" * 60)
    print("TESTING COMPLETION EVENT ENDPOINT")
    print("=" * 60)
    print(f"URL: {erp_url}")
    print("Method: POST")
    print(f"Timeout: {timeout_seconds}s")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("REQUEST PAYLOAD:")
    print(json.dumps(test_payload, indent=2))
    print()
    print("=" * 60)

    if dry_run:
        print("DRY RUN: request was not sent.")
        return True

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    auth = None
    if erp_username and erp_password:
        auth = (erp_username, erp_password)
        print(f"Using Basic Auth: {erp_username}:***")

    try:
        print("\nSending request...")
        response = requests.post(
            erp_url,
            json=test_payload,
            headers=headers,
            auth=auth,
            timeout=timeout_seconds,
        )

        print(f"\nHTTP Status: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        print("\nRESPONSE BODY:")
        print("-" * 60)

        body = response.text
        if body.startswith("\ufeff"):
            body = body[1:]
            print("(Stripped UTF-8 BOM)")

        try:
            print(body)
        except UnicodeEncodeError:
            print(body.encode("utf-8", errors="replace").decode("utf-8"))
        print("-" * 60)

        try:
            response_data = json.loads(body)
            print("\nPARSED JSON:")
            print(json.dumps(response_data, indent=2))

            if response_data.get("status") == "success":
                print("\n[OK] SUCCESS: Endpoint returned status=success")
                return True

            print(f"\n[FAIL] Endpoint returned status={response_data.get('status')}")
            return False
        except json.JSONDecodeError as exc:
            print(f"\n[WARN] Could not parse JSON: {exc}")
            print("Raw response shown above.")
            return response.status_code == 200

    except requests.ConnectionError as exc:
        print(f"\n[ERROR] CONNECTION ERROR: {exc}")
        print("Check whether the configured ERP host is reachable:")
        print(f"  curl -v {erp_url}")
        return False
    except requests.Timeout as exc:
        print(f"\n[ERROR] TIMEOUT: {exc}")
        print("The ERP server took too long to respond.")
        return False
    except requests.HTTPError as exc:
        print(f"\n[ERROR] HTTP ERROR: {exc}")
        return False
    except Exception as exc:
        print(f"\n[ERROR] UNEXPECTED ERROR: {exc}")
        return False


if __name__ == "__main__":
    args = parse_args()
    success = test_completion_event(
        dry_run=args.dry_run,
        url_override=args.url,
        timeout_override=args.timeout,
        message_id_override=args.message_id,
        invoice_id_override=args.invoice_id,
        timestamp_override=args.timestamp,
    )
    print("\n" + "=" * 60)
    if success:
        print("TEST RESULT: [OK] PASSED")
    else:
        print("TEST RESULT: [FAIL] FAILED")
    print("=" * 60)
