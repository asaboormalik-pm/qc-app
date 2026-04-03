#!/usr/bin/env python3
"""Diagnostic script to identify connection issues."""

import socket
import sys
import os

def test_network_connectivity():
    """Test basic network connectivity."""
    print("=" * 60)
    print("NETWORK CONNECTIVITY DIAGNOSTIC")
    print("=" * 60)

    # Test 1: Socket creation
    print("\n[Test 1] Socket Creation")
    try:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print("[OK] Socket created successfully")
        test_socket.close()
    except Exception as e:
        print(f"[ERROR] Socket creation failed: {e}")
        return False

    # Test 2: Supabase connection
    print("\n[Test 2] Supabase Edge Function")
    try:
        import requests
        url = "https://wktfsmiclvyhjpkibgis.supabase.co/functions/v1/print-agent"
        print(f"[INFO] Testing connection to: {url}")
        response = requests.get(url, timeout=5)
        print(f"[OK] Supabase reachable (status {response.status_code})")
    except Exception as e:
        print(f"[ERROR] Supabase connection failed: {e}")

    # Test 3: ERP endpoint
    print("\n[Test 3] ERP Endpoint (192.168.16.201:80)")
    try:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.settimeout(3)
        test_socket.connect(('192.168.16.201', 80))
        print("[OK] Connected to ERP endpoint")
        test_socket.close()
    except socket.timeout:
        print("[TIMEOUT] Connection timed out")
    except ConnectionRefusedError:
        print("[REFUSED] Connection refused")
    except PermissionError as e:
        print(f"[PERMISSION] Permission denied: {e}")
    except Exception as e:
        print(f"[ERROR] {e}")

    # Test 4: Local ports
    print("\n[Test 4] Common Local Ports")
    for port in [9100, 9109]:
        try:
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.settimeout(1)
            test_socket.connect(('127.0.0.1', port))
            print(f"[LISTENING] Port {port} is listening on localhost")
            test_socket.close()
        except:
            print(f"[AVAILABLE] Port {port} is available (not listening)")

    return True

def test_keyring():
    """Test Windows Credential Manager access."""
    print("\n" + "=" * 60)
    print("KEYRING / CREDENTIAL MANAGER DIAGNOSTIC")
    print("=" * 60)

    try:
        from print_agent import SecureStorage
        s = SecureStorage()

        # Test storing
        s.set_shared_api_key('diagnostic-test-key')
        print("[OK] Stored value in Windows Credential Manager")

        # Test retrieving
        result = s.get_shared_api_key()
        if result == 'diagnostic-test-key':
            print("[OK] Retrieved value from Windows Credential Manager")
        else:
            print(f"[ERROR] Retrieved wrong value: {result}")

        # Test clearing
        s.clear_all()
        print("[OK] Cleared Windows Credential Manager")

        return True
    except Exception as e:
        print(f"[ERROR] Keyring/Credential Manager failed: {e}")
        print(f"       Error type: {type(e).__name__}")
        if "Permission" in str(e) or "denied" in str(e).lower():
            print("       This may require administrator privileges")
        return False

def test_config_loading():
    """Test configuration loading."""
    print("\n" + "=" * 60)
    print("CONFIG LOADING DIAGNOSTIC")
    print("=" * 60)

    try:
        from print_agent import load_config, LocalConfigStore

        # Check which config path will be used
        store = LocalConfigStore()
        if store.is_paired():
            print("[INFO] Connector is PAIRED - will load from app-data + keychain")
        else:
            print("[INFO] Connector is NOT PAIRED - will load from .env (legacy mode)")

        # Try loading config
        config = load_config()
        print(f"[OK] Config loaded successfully")
        print(f"      Print Agent URL: {config.print_agent_url[:50]}...")
        print(f"      Workstation ID: {config.workstation_id}")
        print(f"      ERP Enabled: {config.erp_enabled}")

        return True
    except Exception as e:
        print(f"[ERROR] Config loading failed: {e}")
        print(f"       Error type: {type(e).__name__}")
        return False

def main():
    """Run all diagnostics."""
    print("\n" + "=" * 60)
    print("QC CONNECTOR DIAGNOSTIC TOOL")
    print("=" * 60)
    print("This will test network connectivity, keyring access, and config loading.")
    print()

    results = {
        "network": test_network_connectivity(),
        "keyring": test_keyring(),
        "config": test_config_loading(),
    }

    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 60)

    for test_name, passed in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {test_name.upper()}")

    print("\n" + "=" * 60)
    print("NEXT STEPS")
    print("=" * 60)

    if not results["network"]:
        print("- Check your internet connection")
        print("- Check firewall settings")
        print("- Try temporarily disabling antivirus")

    if not results["keyring"]:
        print("- Run as Administrator (right-click -> Run as administrator)")
        print("- Check Windows Credential Manager service is running")

    if not results["config"]:
        print("- Check .env file exists and is valid")
        print("- Check required environment variables are set")

    print()
    print("For detailed error analysis, review the output above.")
    print()

if __name__ == "__main__":
    main()
