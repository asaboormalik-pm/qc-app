#!/usr/bin/env python3
"""Manual pairing-flow smoke script.

This script is intentionally env-driven. It does not ship with embedded URLs,
API keys, or workstation identifiers.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import requests


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def print_section(title: str) -> None:
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def test_pair_endpoint(code: str) -> Dict[str, Any]:
    print_section("TEST 1: Pair Endpoint")

    base_url = require_env("TEST_PAIRING_SUPABASE_URL").rstrip("/")
    shared_api_key = require_env("TEST_PAIRING_SHARED_API_KEY")
    url = f"{base_url}/functions/v1/print-agent?action=pair"

    payload = {
        "code": code,
        "osType": "windows",
        "appVersion": "1.0.0",
    }

    print(f"[INFO] Sending pair request to: {url}")
    print(f"[INFO] Payload: {json.dumps(payload, indent=2)}")

    headers = {
        "X-API-Key": shared_api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"[INFO] Response status: {response.status_code}")
        data = response.json()
    except requests.exceptions.Timeout:
        print("[ERROR] Request timed out")
        return {"success": False, "error": "timeout"}
    except requests.exceptions.ConnectionError as exc:
        print(f"[ERROR] Connection error: {exc}")
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        print(f"[ERROR] Unexpected error: {exc}")
        return {"success": False, "error": str(exc)}

    if response.status_code == 200 and data.get("success"):
        print("[SUCCESS] Pair successful")
        return {
            "success": True,
            "control_plane_token": data.get("controlPlaneToken"),
            "workstation_id": data.get("workstationId"),
            "warehouse_id": data.get("warehouseId"),
        }

    print(f"[ERROR] Pair failed: {json.dumps(data, indent=2)}")
    return {"success": False, "error": data}


def test_heartbeat_endpoint(workstation_id: str, control_plane_token: str) -> bool:
    print_section("TEST 2: Heartbeat Endpoint")

    base_url = require_env("TEST_PAIRING_SUPABASE_URL").rstrip("/")
    shared_api_key = require_env("TEST_PAIRING_SHARED_API_KEY")
    url = f"{base_url}/functions/v1/print-agent?action=heartbeat"

    payload = {
        "workstationId": workstation_id,
        "status": "online",
        "appVersion": "1.0.0",
        "osType": "windows",
        "lastError": None,
    }
    headers = {
        "X-API-Key": shared_api_key,
        "X-Control-Plane-Token": control_plane_token,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[INFO] Response status: {response.status_code}")
        return response.status_code == 200
    except Exception as exc:
        print(f"[ERROR] Heartbeat error: {exc}")
        return False


def test_config_endpoint(workstation_id: str, control_plane_token: str) -> bool:
    print_section("TEST 3: Config Endpoint")

    base_url = require_env("TEST_PAIRING_SUPABASE_URL").rstrip("/")
    shared_api_key = require_env("TEST_PAIRING_SHARED_API_KEY")
    url = f"{base_url}/functions/v1/print-agent?action=config&workstation_id={workstation_id}"

    headers = {
        "X-API-Key": shared_api_key,
        "X-Control-Plane-Token": control_plane_token,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"[INFO] Response status: {response.status_code}")
        return response.status_code == 200
    except Exception as exc:
        print(f"[ERROR] Config error: {exc}")
        return False


def main() -> int:
    print("\nThis is a manual smoke script for the current pairing contract.")
    print("Required env vars:")
    print("  TEST_PAIRING_SUPABASE_URL")
    print("  TEST_PAIRING_SHARED_API_KEY")
    print("  TEST_PAIRING_CODE")

    try:
        pairing_code = require_env("TEST_PAIRING_CODE")
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        return 1

    pair_result = test_pair_endpoint(pairing_code)
    if not pair_result.get("success"):
        print("\n[FAILED] Pairing failed - cannot continue with remaining tests")
        return 1

    control_plane_token = pair_result.get("control_plane_token")
    workstation_id = pair_result.get("workstation_id")
    heartbeat_success = test_heartbeat_endpoint(workstation_id, control_plane_token)
    config_success = test_config_endpoint(workstation_id, control_plane_token)

    print_section("TEST SUMMARY")
    print(f"[{'PASS' if pair_result.get('success') else 'FAIL'}] Pair Endpoint")
    print(f"[{'PASS' if heartbeat_success else 'FAIL'}] Heartbeat Endpoint")
    print(f"[{'PASS' if config_success else 'FAIL'}] Config Endpoint")

    return 0 if pair_result.get("success") and heartbeat_success and config_success else 1


if __name__ == "__main__":
    sys.exit(main())
