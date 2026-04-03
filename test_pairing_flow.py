#!/usr/bin/env python3
"""
Local test script to verify the pairing flow works.

This script tests the edge function pairing endpoints locally before
implementing the full frontend.
"""

import requests
import json
import sys
import time
from typing import Dict, Any

# Configuration - update these for your local setup
SUPABASE_URL = "https://wktfsmiclvyhjpkibgis.supabase.co"
SHARED_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXgjM0dHJQYZ3r0k2YLTCoSFfYqA8g"

def print_section(title: str):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)

def test_generate_pairing_code() -> str:
    """Test generating a pairing code via SQL (simulating frontend)."""
    print_section("TEST 1: Generate Pairing Code")

    # For now, we'll use a hardcoded code for testing
    # In production, the frontend would generate this via the edge function
    test_code = "TEST01"

    print(f"[INFO] Using test pairing code: {test_code}")
    print("[NOTE] In production, frontend would generate code via edge function")

    # TODO: Implement actual code generation via edge function
    # For now, we'll manually insert into the database for testing

    return test_code

def test_pair_endpoint(code: str) -> Dict[str, Any]:
    """Test the pair endpoint of the edge function."""
    print_section("TEST 2: Pair Endpoint")

    url = f"{SUPABASE_URL}/functions/v1/print-agent?action=pair"

    # Simulate Python connector pairing request
    payload = {
        "code": code,
        "workstationId": "test-workstation-123",
        "stationName": "Test Station",
        "osType": "windows",
        "appVersion": "1.0.0"
    }

    print(f"[INFO] Sending pair request to: {url}")
    print(f"[INFO] Payload: {json.dumps(payload, indent=2)}")

    headers = {
        "Authorization": f"Bearer {SHARED_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"[INFO] Response status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"[SUCCESS] Pair successful!")
            print(f"[INFO] Response: {json.dumps(data, indent=2)}")

            # Extract important data
            control_plane_token = data.get("controlPlaneToken")
            config = data.get("config", {})
            warehouse_id = data.get("warehouseId")

            print(f"\n[INFO] Control Plane Token: {control_plane_token[:20]}...")
            print(f"[INFO] Warehouse ID: {warehouse_id}")
            print(f"[INFO] Config keys: {list(config.keys())}")

            return {
                "success": True,
                "control_plane_token": control_plane_token,
                "warehouse_id": warehouse_id,
                "workstation_id": payload["workstationId"]
            }
        else:
            error_data = response.json()
            print(f"[ERROR] Pair failed!")
            print(f"[ERROR] Response: {json.dumps(error_data, indent=2)}")
            return {"success": False, "error": error_data}

    except requests.exceptions.Timeout:
        print("[ERROR] Request timed out")
        return {"success": False, "error": "timeout"}
    except requests.exceptions.ConnectionError as e:
        print(f"[ERROR] Connection error: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        return {"success": False, "error": str(e)}

def test_heartbeat_endpoint(workstation_id: str, control_plane_token: str) -> bool:
    """Test the heartbeat endpoint."""
    print_section("TEST 3: Heartbeat Endpoint")

    url = f"{SUPABASE_URL}/functions/v1/print-agent?action=heartbeat"

    payload = {
        "workstationId": workstation_id,
        "status": "online",
        "appVersion": "1.0.0",
        "osType": "windows",
        "lastError": None
    }

    print(f"[INFO] Sending heartbeat request to: {url}")

    headers = {
        "Authorization": f"Bearer {SHARED_API_KEY}",
        "X-Control-Plane-Token": control_plane_token,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[INFO] Response status: {response.status_code}")

        if response.status_code == 200:
            print("[SUCCESS] Heartbeat successful!")
            return True
        else:
            error_data = response.json()
            print(f"[ERROR] Heartbeat failed!")
            print(f"[ERROR] Response: {json.dumps(error_data, indent=2)}")
            return False

    except Exception as e:
        print(f"[ERROR] Heartbeat error: {e}")
        return False

def test_config_endpoint(workstation_id: str, control_plane_token: str) -> bool:
    """Test the config endpoint."""
    print_section("TEST 4: Config Endpoint")

    url = f"{SUPABASE_URL}/functions/v1/print-agent?action=config&workstation_id={workstation_id}"

    print(f"[INFO] Sending config request to: {url}")

    headers = {
        "Authorization": f"Bearer {SHARED_API_KEY}",
        "X-Control-Plane-Token": control_plane_token
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"[INFO] Response status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print("[SUCCESS] Config fetch successful!")
            print(f"[INFO] Config keys: {list(data.get('config', {}).keys())}")
            return True
        else:
            error_data = response.json()
            print(f"[ERROR] Config fetch failed!")
            print(f"[ERROR] Response: {json.dumps(error_data, indent=2)}")
            return False

    except Exception as e:
        print(f"[ERROR] Config error: {e}")
        return False

def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print(" LOCAL PAIRING FLOW TEST")
    print("=" * 60)
    print("\nThis script tests the edge function pairing endpoints locally.")
    print("Make sure the edge function has been deployed with the new")
    print("pair, heartbeat, and config actions.\n")

    # Test 1: Generate pairing code
    pairing_code = test_generate_pairing_code()

    # Test 2: Pair endpoint
    pair_result = test_pair_endpoint(pairing_code)

    if not pair_result.get("success"):
        print("\n[FAILED] Pairing failed - cannot continue with remaining tests")
        sys.exit(1)

    # Test 3: Heartbeat endpoint
    control_plane_token = pair_result.get("control_plane_token")
    workstation_id = pair_result.get("workstation_id")

    heartbeat_success = test_heartbeat_endpoint(workstation_id, control_plane_token)

    # Test 4: Config endpoint
    config_success = test_config_endpoint(workstation_id, control_plane_token)

    # Summary
    print_section("TEST SUMMARY")
    print(f"✓ Pairing Code: {pairing_code}")
    print(f"{'✓' if pair_result['success'] else '✗'} Pair Endpoint: {'PASSED' if pair_result['success'] else 'FAILED'}")
    print(f"{'✓' if heartbeat_success else '✗'} Heartbeat Endpoint: {'PASSED' if heartbeat_success else 'FAILED'}")
    print(f"{'✓' if config_success else '✗'} Config Endpoint: {'PASSED' if config_success else 'FAILED'}")

    all_passed = pair_result['success'] and heartbeat_success and config_success

    if all_passed:
        print("\n[SUCCESS] All tests passed! The pairing flow is working correctly.")
        return 0
    else:
        print("\n[FAILED] Some tests failed. Please check the edge function implementation.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
