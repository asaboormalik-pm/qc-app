#!/usr/bin/env python3
"""
Local test script for ConnectorManager pairing logic.

This script tests the pairing functionality without making actual HTTP requests
by mocking the edge function responses.
"""

import sys
import os
import json
import uuid
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path to import print_agent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from print_agent import LocalConfigStore, SecureStorage, ConnectorManager

def print_section(title: str):
    """Print a section header."""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)

def test_local_config_store():
    """Test LocalConfigStore functionality."""
    print_section("TEST 1: LocalConfigStore")

    try:
        store = LocalConfigStore()

        # Test app data directory
        app_data_dir = store.get_app_data_dir()
        print(f"[OK] App data directory: {app_data_dir}")

        # Test workstation ID generation/persistence
        workstation_id = store.get_workstation_id()
        print(f"[OK] Workstation ID: {workstation_id}")

        # Verify it's a valid UUID
        uuid.UUID(workstation_id)
        print("[OK] Workstation ID is valid UUID")

        # Test state loading
        state = store.load_state()
        print(f"[OK] State loaded: {json.dumps(state, indent=2)}")

        # Test is_paired (should be False initially)
        assert not store.is_paired(), "[ERROR] New store should not be paired"
        print("[OK] is_paired() returns False for new store")

        # Test config save/load
        test_config = {
            "print": {"enabled": True, "pollIntervalSeconds": 2},
            "erp": {"enabled": True}
        }
        store.save_config(test_config)
        loaded_config = store.load_config()
        assert loaded_config == test_config, "[ERROR] Config mismatch"
        print("[OK] Config save/load works")

        return True

    except Exception as e:
        print(f"[ERROR] LocalConfigStore test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_secure_storage():
    """Test SecureStorage (keychain) functionality."""
    print_section("TEST 2: SecureStorage (Keychain)")

    try:
        secure = SecureStorage()

        # Test storing and retrieving shared API key
        test_key = "test-api-key-12345"
        secure.set_shared_api_key(test_key)
        retrieved_key = secure.get_shared_api_key()

        assert retrieved_key == test_key, "[ERROR] API key mismatch"
        print(f"[OK] Shared API key stored/retrieved: {retrieved_key}")

        # Test storing and retrieving control plane token
        test_token = str(uuid.uuid4())
        secure.set_control_plane_token(test_token)
        retrieved_token = secure.get_control_plane_token()

        assert retrieved_token == test_token, "[ERROR] Token mismatch"
        print(f"[OK] Control plane token stored/retrieved: {retrieved_token[:20]}...")

        # Test storing ERP basic auth
        test_user = "test-user"
        test_pass = "test-password"
        secure.set_erp_basic_auth(test_user, test_pass)
        retrieved_user, retrieved_pass = secure.get_erp_basic_auth()

        assert retrieved_user == test_user, "[ERROR] Username mismatch"
        assert retrieved_pass == test_pass, "[ERROR] Password mismatch"
        print(f"[OK] ERP basic auth stored/retrieved: {retrieved_user}")

        # Cleanup
        secure.clear_all()
        print("[OK] Cleared all credentials")

        return True

    except Exception as e:
        print(f"[ERROR] SecureStorage test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_connector_manager_pairing_success():
    """Test ConnectorManager pairing with mocked successful response."""
    print_section("TEST 3: ConnectorManager Pairing (Success)")

    try:
        # Mock response from edge function
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "config": {
                "print": {"enabled": True, "pollIntervalSeconds": 2},
                "erp": {"enabled": True},
                "edgeFunctions": {
                    "apiKey": "edge-function-key",
                    "sharedUrl": "https://test.supabase.co/functions/v1/print-agent"
                }
            },
            "controlPlaneToken": str(uuid.uuid4()),
            "warehouse_id": "WH001",  # snake_case to match Python code
            "station_name": "Test Warehouse Station"  # snake_case to match Python code
        }

        with patch('requests.post', return_value=mock_response):
            manager = ConnectorManager(
                api_base="https://test.supabase.co/functions/v1/print-agent",
                shared_api_key="test-shared-key"
            )

            # Test pairing
            result = manager.pair_with_code("TEST123", "Test Station")

            assert result.get("success"), "[ERROR] Pairing should succeed"
            print(f"[OK] Pairing successful: {result}")

            # Verify state was updated
            state = manager.store.load_state()
            assert state.get("is_paired"), "[ERROR] State should show paired"
            assert state.get("warehouse_id") == "WH001", "[ERROR] Warehouse ID mismatch"
            print(f"[OK] State updated: {json.dumps(state, indent=2)}")

            # Verify control plane token was stored
            token = manager.secure.get_control_plane_token()
            assert token is not None, "[ERROR] Control plane token should be stored"
            print(f"[OK] Control plane token stored: {token[:20]}...")

            # Verify config was stored
            config = manager.store.load_config()
            assert config is not None, "[ERROR] Config should be stored"
            assert config.get("print", {}).get("enabled") == True, "[ERROR] Config content mismatch"
            print(f"[OK] Config stored: {list(config.keys())}")

            # Verify API key was stored in secure storage
            api_key = manager.secure.get_shared_api_key()
            assert api_key == "edge-function-key", "[ERROR] API key mismatch"
            print(f"[OK] Edge function API key stored: {api_key}")

            return True

    except Exception as e:
        print(f"[ERROR] ConnectorManager pairing test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_connector_manager_pairing_failure():
    """Test ConnectorManager pairing with failure response."""
    print_section("TEST 4: ConnectorManager Pairing (Failure)")

    try:
        # Clean up any existing paired state first
        store = LocalConfigStore()
        state = store.load_state()
        state["is_paired"] = False
        state["paired_at"] = None
        state["warehouse_id"] = None
        state["station_name"] = None
        store.save_state(state)

        # Mock error response from edge function
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "success": False,
            "error": "Invalid pairing code"
        }

        with patch('requests.post', return_value=mock_response):
            manager = ConnectorManager(
                api_base="https://test.supabase.co/functions/v1/print-agent",
                shared_api_key="test-shared-key"
            )

            # Test pairing with invalid code
            result = manager.pair_with_code("INVALID")

            assert not result.get("success"), "[ERROR] Pairing should fail"
            print(f"[OK] Pairing correctly failed: {result}")

            # Verify state was NOT updated
            state = manager.store.load_state()
            assert not state.get("is_paired", False), "[ERROR] State should NOT show paired"
            print("[OK] State correctly not updated")

            return True

    except Exception as e:
        print(f"[ERROR] ConnectorManager pairing failure test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_connector_manager_config_fetch():
    """Test ConnectorManager config fetch."""
    print_section("TEST 5: ConnectorManager Config Fetch")

    try:
        # First, set up as paired
        manager = ConnectorManager(
            api_base="https://test.supabase.co/functions/v1/print-agent",
            shared_api_key="test-shared-key"
        )

        # Manually set up paired state
        manager.state["is_paired"] = True
        manager.state["warehouse_id"] = "WH001"
        manager.state["paired_at"] = datetime.now(timezone.utc).isoformat()
        manager.store.save_state(manager.state)

        # Store control plane token
        test_token = str(uuid.uuid4())
        manager.secure.set_control_plane_token(test_token)

        # Mock config fetch response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "config": {
                "print": {"enabled": False, "pollIntervalSeconds": 5},
                "erp": {"enabled": False}
            },
            "version": "2024-01-01T00:00:00Z"
        }

        with patch('requests.get', return_value=mock_response):
            config = manager.fetch_config()

            assert config is not None, "[ERROR] Config should be returned"
            assert config.get("print", {}).get("enabled") == False, "[ERROR] Config content mismatch"
            print(f"[OK] Config fetched successfully: {list(config.keys())}")

            # Verify config was updated locally
            stored_config = manager.store.load_config()
            assert stored_config.get("print", {}).get("enabled") == False, "[ERROR] Local config not updated"
            print("[OK] Local config updated")

            return True

    except Exception as e:
        print(f"[ERROR] Config fetch test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_connector_manager_heartbeat():
    """Test ConnectorManager heartbeat."""
    print_section("TEST 6: ConnectorManager Heartbeat")

    try:
        manager = ConnectorManager(
            api_base="https://test.supabase.co/functions/v1/print-agent",
            shared_api_key="test-shared-key"
        )

        # Set up as paired
        manager.state["is_paired"] = True
        manager.state["warehouse_id"] = "WH001"
        manager.store.save_state(manager.state)

        # Store control plane token
        test_token = str(uuid.uuid4())
        manager.secure.set_control_plane_token(test_token)

        # Mock heartbeat response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}

        with patch('requests.post', return_value=mock_response):
            # Test sending heartbeat (should not raise exception)
            manager.send_heartbeat("online")
            print("[OK] Heartbeat sent successfully")

            # Test with error status
            manager.send_heartbeat("error", "Connection lost")
            print("[OK] Heartbeat with error sent successfully")

            return True

    except Exception as e:
        print(f"[ERROR] Heartbeat test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def cleanup_test_data():
    """Clean up test data from previous runs."""
    print_section("CLEANUP: Removing Test Data")

    try:
        store = LocalConfigStore()
        secure = SecureStorage()

        # Clear pairing state
        state = store.load_state()
        state["is_paired"] = False
        state["paired_at"] = None
        state["warehouse_id"] = None
        state["station_name"] = None
        store.save_state(state)

        # Clear credentials
        secure.clear_all()

        # Clear config
        config_file = store.config_file
        if config_file.exists():
            config_file.unlink()

        print("[OK] Test data cleaned up")
        return True

    except Exception as e:
        print(f"[ERROR] Cleanup failed: {e}")
        return False

def main():
    """Run all local tests."""
    print("\n" + "=" * 60)
    print(" LOCAL CONNECTOR PAIRING TESTS")
    print("=" * 60)
    print("\nThese tests verify the ConnectorManager pairing logic works")
    print("correctly without making actual HTTP requests to the edge function.\n")

    # First cleanup any existing test data
    cleanup_test_data()

    results = {
        "LocalConfigStore": test_local_config_store(),
        "SecureStorage": test_secure_storage(),
        "Pairing (Success)": test_connector_manager_pairing_success(),
        "Pairing (Failure)": test_connector_manager_pairing_failure(),
        "Config Fetch": test_connector_manager_config_fetch(),
        "Heartbeat": test_connector_manager_heartbeat()
    }

    # Cleanup after tests
    cleanup_test_data()

    # Summary
    print_section("TEST SUMMARY")
    for test_name, passed in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status}: {test_name}")

    all_passed = all(results.values())

    if all_passed:
        print("\n[SUCCESS] All local tests passed!")
        print("\nThe ConnectorManager pairing logic is working correctly.")
        print("Next steps:")
        print("1. Deploy edge function with pair/heartbeat/config handlers")
        print("2. Test end-to-end with actual edge function")
        return 0
    else:
        print("\n[FAILED] Some tests failed.")
        print("Please fix the issues before deploying.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
