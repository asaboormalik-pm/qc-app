#!/usr/bin/env python3
"""On-prem print agent for Zebra/compatible network printers.

This agent polls an edge function for print jobs, sends ZPL to printers via raw
TCP 9100, and posts completion or failure back to the same edge function.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
import random
import pprint
import threading
import atexit
import json
import base64
import hashlib
import re
import uuid
import platform as platform_module
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal as signal_module

# Platform-specific signal imports
SIGINT = signal_module.SIGINT
SIGTERM = signal_module.SIGTERM
try:
    SIGHUP = signal_module.SIGHUP  # Unix only
except AttributeError:
    SIGHUP = None  # Windows doesn't have SIGHUP

import requests


# Tkinter for setup wizard (imported only when needed)
try:
    import tkinter as tk
    from tkinter import ttk
    from tkinter import messagebox
    # Verify Tkinter can actually create windows (not just import)
    test_root = tk.Tk()
    test_root.withdraw()
    test_root.destroy()
    TKINTER_AVAILABLE = True
except Exception:
    TKINTER_AVAILABLE = False


def pause_for_debug():
    """Pause execution so user can read console messages (only in bundled exe)."""
    if getattr(sys, 'frozen', False):
        # Running as bundled executable
        print()
        print("=" * 60)
        print("Press Enter to exit...")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass


def show_error_message(title: str, message: str) -> None:
    """Show error message in GUI message box (if available) or console."""
    if TKINTER_AVAILABLE:
        try:
            # Create a hidden root window for the message box
            root = tk.Tk()
            root.withdraw()  # Hide the main window
            messagebox.showerror(title, message)
            root.destroy()
        except Exception:
            # If message box fails, fall back to console
            print(f"ERROR: {title}")
            print(f"  {message}")
    else:
        print(f"ERROR: {title}")
        print(f"  {message}")


def show_info_message(title: str, message: str) -> None:
    """Show info message in GUI message box (if available) or console."""
    if TKINTER_AVAILABLE:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(title, message)
            root.destroy()
        except Exception:
            print(f"{title}")
            print(f"  {message}")
    else:
        print(f"{title}")
        print(f"  {message}")


def get_exe_dir() -> Path:
    """Get the directory containing the executable (for bundled exe) or script (for dev)."""
    if getattr(sys, 'frozen', False):
        # Running as bundled executable
        return Path(sys.executable).parent
    else:
        # Running as script - use current directory
        return Path.cwd()


def ensure_env_file_exists() -> None:
    """Create .env file from .env.example if it doesn't exist.

    Searches in multiple locations:
    1. Exe directory (for bundled apps)
    2. Current working directory
    3. AppData directory (for paired state)
    """
    from pathlib import Path

    exe_dir = get_exe_dir()
    cwd = Path.cwd()

    internal_dir = exe_dir / "_internal"

    # Search paths in priority order
    search_paths = [
        exe_dir,           # Where the exe is located
        internal_dir,      # PyInstaller onedir data files
        cwd,               # Current working directory
    ]

    env_file = None
    env_example = None

    for search_path in search_paths:
        test_env = search_path / '.env'
        test_example = search_path / '.env.example'
        if test_env.exists():
            env_file = test_env
            break
        if test_example.exists():
            env_example = test_example

    # If .env already exists, we're done
    if env_file and env_file.exists():
        return

    # Determine where to create .env (prefer exe directory)
    target_dir = exe_dir if exe_dir.exists() else cwd
    env_file = target_dir / '.env'

    # Try to copy from .env.example
    if env_example and env_example.exists():
        import shutil
        shutil.copy(env_example, env_file)
        print(f"[CONFIG] Created .env file from .env.example at: {env_file}")
    else:
        # Create a minimal .env file with required variables
        minimal_env = """# QC Print Agent Configuration
# Generated automatically on first run

PRINT_AGENT_CALLBACK_URL=https://your-project.supabase.co/functions/v1/print-agent
PRINT_AGENT_API_KEY=your-api-key-here

# Print settings
PRINTER_PORT=9100
PRINTER_TIMEOUT_SECONDS=5

# Polling
POLL_INTERVAL_SECONDS=2
MAX_CONCURRENT_JOBS=3

# ERP (optional - remove if not using)
# ERP_ENABLED=true
"""
        with open(env_file, 'w') as f:
            f.write(minimal_env)
        print(f"[CONFIG] Created .env file with default configuration at: {env_file}")

    print()
    print("IMPORTANT: The pairing wizard will automatically configure these values.")
    print("            Just enter your pairing code and click Connect.")
    print()


DEFAULT_HTTP_TIMEOUT_SECONDS = 10
DEFAULT_CALLBACK_RETRIES = 3


class ConfigError(Exception):
    """Configuration error exception."""
    pass


class LocalConfigStore:
    """Handles persistent storage for connector state and config in platform-specific app-data directories."""

    @staticmethod
    def get_app_data_dir() -> Path:
        """Returns platform-specific app data directory."""
        system = platform_module.system().lower()
        if system == "windows":
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
            return Path(base) / "QCConnector"
        elif system == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
            return Path(base) / "QCConnector"
        else:  # linux, etc.
            base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            return Path(base) / "qc-connector"

    def __init__(self):
        self.app_data_dir = self.get_app_data_dir()
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.app_data_dir / "config.json"
        self.state_file = self.app_data_dir / "state.json"
        self.log_file = self.app_data_dir / "print_agent.log"
        self.pid_file = self.app_data_dir / "print_agent.pid"

    def save_config(self, config: Dict[str, Any]) -> None:
        """Save non-secret connector configuration to JSON."""
        non_secret_config = self._strip_secrets(config)
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(non_secret_config, f, indent=2)

    def save_config_to_env(self, config: Dict[str, Any]) -> None:
        """Save configuration to .env file for next run."""
        env_file = Path('.env')
        env_vars = []

        if not isinstance(config, dict):
            logging.warning("save_config_to_env expected dict config, got %s", type(config))
            return

        def get_config_value(*keys: str) -> Optional[Any]:
            for key in keys:
                value = config.get(key)
                if value not in (None, ""):
                    return value
            return None

        # Add PRINT_AGENT_CALLBACK_URL
        print_agent_url = get_config_value("print_agent_url", "printAgentUrl")
        if not print_agent_url:
            edge_functions = config.get("edgeFunctions", {})
            if isinstance(edge_functions, dict):
                print_agent_url = edge_functions.get("sharedUrl") or edge_functions.get("printAgentUrl")
        if print_agent_url:
            env_vars.append(f'PRINT_AGENT_CALLBACK_URL={print_agent_url}')

        # Add PRINT_AGENT_API_KEY
        print_agent_api_key = get_config_value("print_agent_api_key", "printAgentApiKey", "apiKey")
        if not print_agent_api_key:
            edge_functions = config.get("edgeFunctions", {})
            if isinstance(edge_functions, dict):
                print_agent_api_key = edge_functions.get("apiKey")
        if print_agent_api_key:
            env_vars.append(f'PRINT_AGENT_API_KEY={print_agent_api_key}')

        # Add other non-secret settings
        poll_interval_seconds = get_config_value("poll_interval_seconds")
        max_concurrent_jobs = get_config_value("max_concurrent_jobs")
        printer_port = get_config_value("printer_port")
        printer_timeout_seconds = get_config_value("printer_timeout_seconds")

        if poll_interval_seconds is not None:
            env_vars.append(f'POLL_INTERVAL_SECONDS={poll_interval_seconds}')
        if max_concurrent_jobs is not None:
            env_vars.append(f'MAX_CONCURRENT_JOBS={max_concurrent_jobs}')
        if printer_port is not None:
            env_vars.append(f'PRINTER_PORT={printer_port}')
        if printer_timeout_seconds is not None:
            env_vars.append(f'PRINTER_TIMEOUT_SECONDS={printer_timeout_seconds}')

        if env_vars:
            with open(env_file, 'w', encoding='utf-8') as f:
                f.write('# QC Print Agent Configuration\n')
                f.write('# Auto-generated by setup wizard\n')
                f.write('\n')
                f.write('\n'.join(env_vars))
                f.write('\n')

    def load_config(self) -> Optional[Dict[str, Any]]:
        """Load non-secret connector configuration from JSON."""
        if self.config_file.exists():
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    def save_state(self, state: Dict[str, Any]) -> None:
        """Save runtime state to JSON."""
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

    def load_state(self) -> Dict[str, Any]:
        """Load runtime state, creating default if missing."""
        if self.state_file.exists():
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)

        # Default state - generate workstation ID once
        default_state = {
            "workstation_id": str(uuid.uuid4()),
            "is_paired": False,
            "paired_at": None,
            "warehouse_id": None,
            "station_name": None
        }
        self.save_state(default_state)
        return default_state

    def is_paired(self) -> bool:
        """Check if connector has been paired."""
        state = self.load_state()
        return state.get("is_paired", False)

    def get_workstation_id(self) -> str:
        """Get workstation ID (generated once, persisted)."""
        state = self.load_state()
        return state["workstation_id"]

    def save_workstation_id(self, workstation_id: str) -> None:
        """Save workstation ID to state (used when backend assigns one during pairing)."""
        state = self.load_state()
        state["workstation_id"] = workstation_id
        self.save_state(state)

    def get_log_path(self) -> Path:
        """Get log file path in app-data directory."""
        return self.log_file

    def get_pid_path(self) -> Path:
        """Get PID file path in app-data directory."""
        return self.pid_file

    def reset_paired_state(self) -> None:
        """Reset paired state - called when device is unpaired from server."""
        state = self.load_state()
        state["is_paired"] = False
        state["paired_at"] = None
        state["warehouse_id"] = None
        state["station_name"] = None
        self.save_state(state)
        logging.info("[CONNECTOR] Paired state reset - device unpaired from server")

    def clear_local_runtime_state(self) -> None:
        """Remove cached local files that should not survive a reset flow."""
        for path in (self.config_file, self.pid_file):
            try:
                if path.exists():
                    path.unlink()
                    logging.info("Removed local runtime file: %s", path)
            except Exception as exc:
                logging.warning("Could not remove local runtime file %s: %s", path, exc)

    def _strip_secrets(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Remove secrets from config before saving to JSON."""
        # Deep copy to avoid modifying original
        config = json.loads(json.dumps(config))

        # Strip ERP secrets
        if "erp_auth_basic_password" in config:
            config["erp_auth_basic_password"] = "***REDACTED***"
        if "erp_auth_bearer_token" in config:
            config["erp_auth_bearer_token"] = "***REDACTED***"
        if "print_agent_api_key" in config:
            config["print_agent_api_key"] = "***REDACTED***"
        if "erp_agent_api_key" in config:
            config["erp_agent_api_key"] = "***REDACTED***"

        return config


class SecureStorage:
    """Handles secure storage of secrets using OS keychain (Windows Credential Manager / macOS Keychain)."""

    def __init__(self, service_name: str = "QCConnector"):
        self.service_name = service_name

    def set_shared_api_key(self, api_key: str) -> None:
        """Store shared edge function API key."""
        import keyring
        keyring.set_password(self.service_name, "shared_api_key", api_key)

    def get_shared_api_key(self) -> Optional[str]:
        """Retrieve shared edge function API key."""
        import keyring
        return keyring.get_password(self.service_name, "shared_api_key")

    def set_control_plane_token(self, token: str) -> None:
        """Store per-device control-plane token."""
        import keyring
        keyring.set_password(self.service_name, "control_plane_token", token)

    def get_control_plane_token(self) -> Optional[str]:
        """Retrieve per-device control-plane token."""
        import keyring
        return keyring.get_password(self.service_name, "control_plane_token")

    def set_erp_basic_auth(self, username: str, password: str) -> None:
        """Store ERP basic auth credentials."""
        import keyring
        keyring.set_password(self.service_name, "erp_basic_username", username)
        keyring.set_password(self.service_name, "erp_basic_password", password)

    def get_erp_basic_auth(self) -> tuple[Optional[str], Optional[str]]:
        """Retrieve ERP basic auth credentials."""
        import keyring
        username = keyring.get_password(self.service_name, "erp_basic_username")
        password = keyring.get_password(self.service_name, "erp_basic_password")
        return username, password

    def set_erp_bearer_token(self, token: str) -> None:
        """Store ERP bearer token."""
        import keyring
        keyring.set_password(self.service_name, "erp_bearer_token", token)

    def get_erp_bearer_token(self) -> Optional[str]:
        """Retrieve ERP bearer token."""
        import keyring
        return keyring.get_password(self.service_name, "erp_bearer_token")

    def clear_all(self) -> None:
        """Clear all stored credentials (for unpairing)."""
        import keyring
        try:
            keyring.delete_password(self.service_name, "shared_api_key")
        except:
            pass
        try:
            keyring.delete_password(self.service_name, "control_plane_token")
        except:
            pass
        try:
            keyring.delete_password(self.service_name, "erp_basic_username")
        except:
            pass
        try:
            keyring.delete_password(self.service_name, "erp_basic_password")
        except:
            pass
        try:
            keyring.delete_password(self.service_name, "erp_bearer_token")
        except:
            pass


@dataclass
class ErpEndpointConfig:
    url: str
    method: str = "POST"
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS


@dataclass
class Config:
    print_agent_url: str
    print_agent_api_key: str
    poll_interval_seconds: float = 2.0
    printer_port: int = 9100
    printer_timeout_seconds: float = 5.0
    max_concurrent_jobs: int = 3  # Support 2-3 printers per station
    workstation_id: str = ""  # Unique identifier for this workstation (for tracking)
    erp_enabled: bool = False
    erp_endpoints: Dict[str, ErpEndpointConfig] = field(default_factory=dict)
    erp_auth_mode: str = "none"
    erp_auth_bearer_token: Optional[str] = None
    erp_auth_basic_username: Optional[str] = None
    erp_auth_basic_password: Optional[str] = None
    erp_auth_static_headers: Dict[str, str] = field(default_factory=dict)
    erp_retry_attempts: int = 0
    erp_retry_backoff_seconds: float = 1.0
    erp_default_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS
    # Advanced ERP retry settings
    erp_timeout_max_seconds: float = 30.0
    erp_retry_max_attempts: int = 3
    erp_retry_backoff_base_seconds: float = 1.0
    erp_retry_backoff_max_seconds: float = 60.0
    erp_retry_jitter_seconds: float = 1.0
    # ERP-agent specific settings (separate from print jobs)
    erp_agent_enabled: bool = False
    erp_agent_url: str = ""
    erp_agent_api_key: str = ""
    erp_agent_poll_interval_seconds: float = 2.0
    erp_agent_max_concurrent_requests: int = 2


@dataclass
class ErpBoxFetchRequest:
    """Normalized ERP box fetch request from erp-agent."""
    request_id: str
    invoice_id: str
    quantity: int
    attempts: int
    max_attempts: int
    request_payload: Optional[Dict[str, Any]]
    invoice_message_id: Optional[str]
    client_id: Optional[str]
    warehouse_id: Optional[str]


@dataclass
class GenericErpRequest:
    """Generic ERP request from erp-agent (supports multiple business types)."""
    request_id: str
    business_type: str
    endpoint_key: str
    request_payload: Optional[Dict[str, Any]]
    attempts: int
    max_attempts: int
    # Optional fields (for box fetch compatibility)
    invoice_id: Optional[str] = None
    quantity: Optional[int] = None
    invoice_message_id: Optional[str] = None
    client_id: Optional[str] = None
    warehouse_id: Optional[str] = None


class ErpAgentClient:
    """Client for communicating with the erp-agent edge function."""

    def __init__(self, url: str, api_key: str, workstation_id: str, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS):
        self.url = url
        self.api_key = api_key
        self.workstation_id = workstation_id
        self.timeout = timeout
        self.headers = {
            "X-API-Key": api_key,
            "X-Workstation-Id": workstation_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _format_json_for_log(value: Any) -> str:
        """Return a readable JSON-like string for logging without changing payloads."""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return value
            return json.dumps(parsed, indent=2, ensure_ascii=False)

        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    def poll_requests(self, limit: int) -> List[GenericErpRequest]:
        """Poll for pending ERP requests (supports multiple business types).

        Returns:
            List of normalized GenericErpRequest objects.
        """
        try:
            response = requests.get(
                self.url,
                headers=self.headers,
                params={"action": "poll", "limit": str(limit)},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.ConnectionError as exc:
            logging.warning("[ERP-AGENT] Connection error: %s - will retry", exc)
            return []
        except requests.Timeout as exc:
            logging.warning("[ERP-AGENT] Request timeout: %s - will retry", exc)
            return []
        except requests.HTTPError as exc:
            logging.error("[ERP-AGENT] HTTP error polling requests: %s", exc)
            # Log response body for debugging
            if exc.response is not None:
                try:
                    logging.error("[ERP-AGENT] Response status: %s", exc.response.status_code)
                    logging.error("[ERP-AGENT] Response body (first 500 chars): %s", exc.response.text[:500])
                except Exception as log_exc:
                    logging.error("[ERP-AGENT] Could not log response details: %s", log_exc)
            return []
        except ValueError as exc:
            logging.error("[ERP-AGENT] Invalid JSON response: %s", exc)
            return []

        # Extract requests from response
        raw_requests: List[Dict[str, Any]]
        if isinstance(body, dict):
            raw_requests = body.get("requests") or []
        elif isinstance(body, list):
            raw_requests = body
        else:
            logging.error("[ERP-AGENT] Unexpected poll response format: %s", type(body))
            return []

        # Normalize into GenericErpRequest objects
        fetched_requests = []
        for req in raw_requests:
            try:
                # Extract required routing fields
                business_type = req.get("business_type")
                endpoint_key = req.get("endpoint_key")

                if not business_type:
                    logging.error("[ERP-AGENT] Missing business_type in request: %s", req.get("id"))
                    continue
                if not endpoint_key:
                    logging.error("[ERP-AGENT] Missing endpoint_key in request: %s", req.get("id"))
                    continue

                # Log incoming request with business_type for debugging
                logging.info(
                    "[ERP-AGENT] INCOMING AGENT REQUEST business_type=%s request_id=%s:\n%s",
                    business_type,
                    req.get("id"),
                    self._format_json_for_log(req),
                )

                # For backwards compatibility, support old box fetch format without business_type
                if business_type not in ("erp_box_fetch", "completion_event"):
                    logging.error("[ERP-AGENT] Unknown business_type: %s for request_id=%s",
                                 business_type, req.get("id"))
                    continue

                # Defensive check: skip requests that have exceeded max_attempts
                # This prevents infinite retry loops when backend doesn't properly filter them
                attempts = int(req.get("attempts", 0))
                max_attempts = int(req.get("max_attempts", 3))
                if attempts >= max_attempts:
                    logging.warning(
                        "[ERP-AGENT] Skipping request_id=%s that has exceeded max_attempts (attempts=%d, max_attempts=%d). "
                        "Backend should have filtered this out - marking as failed.",
                        req.get("id"), attempts, max_attempts
                    )
                    # Notify backend to mark as failed (defensive measure)
                    try:
                        self.post_failure(req.get("id"), f"Exceeded max_attempts ({attempts}/{max_attempts})")
                    except Exception as exc:
                        logging.error("[ERP-AGENT] Failed to post failure for exhausted request_id=%s: %s",
                                     req.get("id"), exc)
                    continue

                # Extract request_payload with fallback for completion_event
                # Some backends may send completion data in different fields
                request_payload = req.get("request_payload")
                if request_payload is None:
                    # Try alternative field names for completion events
                    if business_type == "completion_event":
                        request_payload = req.get("payload") or req.get("erp_payload")
                        if request_payload is None:
                            # Build minimal payload from top-level fields as last resort
                            request_payload = {
                                "message_id": req.get("message_id"),
                                "invoice_id": req.get("invoice_id"),
                                "client_id": req.get("client_id"),
                                "operator_id": req.get("operator_id"),
                                "warehouse_id": req.get("warehouse_id"),
                                "boxes": req.get("boxes"),
                                "shortages": req.get("shortages"),
                                "summary": req.get("summary"),
                                "timestamp": req.get("timestamp"),
                            }
                            # Remove None values
                            request_payload = {k: v for k, v in request_payload.items() if v is not None}
                            if request_payload:
                                logging.warning("[ERP-AGENT] request_payload missing for completion_event request_id=%s, built from top-level fields: %s",
                                               req.get("id"), list(request_payload.keys()))

                fetched_requests.append(GenericErpRequest(
                    request_id=req.get("id", ""),
                    business_type=business_type,
                    endpoint_key=endpoint_key,
                    request_payload=request_payload,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    # Optional fields (for box fetch compatibility)
                    invoice_id=req.get("invoice_id"),
                    quantity=int(req.get("quantity", 0)) if req.get("quantity") else None,
                    invoice_message_id=req.get("invoice_message_id"),
                    client_id=req.get("client_id"),
                    warehouse_id=req.get("warehouse_id"),
                ))
            except (ValueError, TypeError) as exc:
                logging.error("[ERP-AGENT] Invalid request format, skipping: %s", exc)

        if fetched_requests:
            logging.info("[ERP-AGENT] Polled %d requests", len(fetched_requests))

        return fetched_requests

    def post_success(self, request_id: str, boxes: List[Dict[str, Any]]) -> None:
        """Post successful box fetch result with boxes to erp-agent."""
        self._post_result({
            "request_id": request_id,
            "business_type": "erp_box_fetch",
            "status": "completed",
            "boxes": boxes,
        })

    def post_failure(self, request_id: str, error_message: str) -> None:
        """Post failure result to erp-agent."""
        self._post_result({
            "request_id": request_id,
            "status": "failed",
            "error_message": error_message,
        })

    def post_completion_success(self, request_id: str, erp_response: Dict[str, Any]) -> None:
        """Post successful completion event result to erp-agent."""
        self._post_result({
            "request_id": request_id,
            "business_type": "completion_event",
            "status": "completed",
            "erp_status_code": 200,
            "erp_response": erp_response,
        })

    def post_completion_failure(self, request_id: str, error_message: str) -> None:
        """Post completion event failure result to erp-agent."""
        self._post_result({
            "request_id": request_id,
            "business_type": "completion_event",
            "status": "failed",
            "error_message": error_message,
        })

    def _post_result(self, payload: Dict[str, Any]) -> None:
        """Post result to erp-agent with retry logic."""
        request_id = payload.get("request_id", "unknown")
        business_type = payload.get("business_type", "unknown")
        payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload_preview = json.dumps(payload, ensure_ascii=False)

        for attempt in range(1, DEFAULT_CALLBACK_RETRIES + 1):
            try:
                response = requests.post(
                    self.url,
                    headers=self.headers,
                    data=payload_bytes,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                logging.info("[ERP-AGENT] Callback succeeded business_type=%s request_id=%s status=%s",
                           business_type, request_id, payload.get("status"))
                logging.info("[ERP-AGENT] Callback payload request_id=%s body=%s",
                           request_id, payload_preview)
                return
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == DEFAULT_CALLBACK_RETRIES:
                    logging.error("[ERP-AGENT] Callback failed after %d attempts for request_id=%s: %s",
                                DEFAULT_CALLBACK_RETRIES, request_id, exc)
                    raise
                logging.warning("[ERP-AGENT] Callback retrying request_id=%s attempt=%d/%d: %s",
                              request_id, attempt, DEFAULT_CALLBACK_RETRIES, exc)
                time.sleep(0.5 * attempt)
            except requests.HTTPError as exc:
                logging.error("[ERP-AGENT] HTTP error in callback for request_id=%s: %s",
                            request_id, exc)
                raise


class ConnectorManager:
    """Handles pairing, config refresh, and heartbeat for installed connector."""

    def __init__(self, api_base: str, shared_api_key: str):
        self.api_base = api_base
        self.shared_api_key = shared_api_key
        self.store = LocalConfigStore()
        self.secure = SecureStorage()
        self.workstation_id = self.store.get_workstation_id()
        self.state = self.store.load_state()

    def pair_with_code(self, pairing_code: str, station_name: str = None) -> Dict[str, Any]:
        """Redeem pairing code and receive initial config from backend.

        The edge function will use the workstation_id from the pairing code (selected by admin),
        so we don't send workstationId in the payload. The response includes the assigned workstationId.
        """

        payload = {
            "code": pairing_code,
            "osType": platform_module.system().lower(),
            "appVersion": "1.0.0"
        }

        try:
            response = requests.post(
                f"{self.api_base}?action=pair",
                headers={"X-API-Key": self.shared_api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    # Store secrets in keychain
                    config = data["config"]
                    self._store_secrets(config)

                    # Store non-secret config in app data
                    self.store.save_config(config)

                    # Save config to .env file for next run
                    self.store.save_config_to_env(config)

                    # Store control-plane token
                    control_plane_token = data.get("controlPlaneToken")
                    if control_plane_token:
                        self.secure.set_control_plane_token(control_plane_token)

                    # Update workstation_id from response (assigned by backend)
                    workstation_id = data.get("workstationId")
                    if workstation_id:
                        self.workstation_id = workstation_id
                        self.store.save_workstation_id(workstation_id)

                    # Update pairing state
                    self.state["is_paired"] = True
                    self.state["paired_at"] = datetime.now(timezone.utc).isoformat()
                    self.state["warehouse_id"] = data.get("warehouseId")
                    self.state["station_name"] = data.get("stationName") or data.get("station_name")
                    self.store.save_state(self.state)

                    logging.info(f"[CONNECTOR] Successfully paired to {data.get('stationName', 'Unknown')} (workstation: {workstation_id})")
                    return {"success": True, "stationName": data.get("stationName"), "workstationId": workstation_id}
                else:
                    error_msg = data.get("error", "Unknown error")
                    logging.warning(f"[CONNECTOR] Pairing failed: {error_msg}")
                    return {"success": False, "error": error_msg}

            # Try to parse error response
            try:
                error_data = response.json()
                error_msg = error_data.get("error", f"HTTP {response.status_code}")
                logging.warning(f"[CONNECTOR] Pairing failed: {error_msg}")
                return {"success": False, "error": error_msg}
            except:
                logging.warning(f"[CONNECTOR] Pairing failed: HTTP {response.status_code}")
                return {"success": False, "error": f"HTTP {response.status_code}"}

        except Exception as e:
            logging.error(f"[CONNECTOR] Pairing error: {e}")
            return {"success": False, "error": str(e)}

    def fetch_config(self) -> Optional[Dict[str, Any]]:
        """Fetch latest config from backend."""

        if not self.store.is_paired():
            return None

        control_plane_token = self.secure.get_control_plane_token()
        if not control_plane_token:
            logging.error("[CONNECTOR] No control-plane token found")
            return None

        try:
            response = requests.get(
                f"{self.api_base}?action=config&workstation_id={self.workstation_id}",
                headers={
                    "X-API-Key": self.shared_api_key,
                    "X-Control-Plane-Token": control_plane_token
                },
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                config = data.get("config")

                # Store new secrets in keychain
                self._store_secrets(config)

                # Update local config cache
                self.store.save_config(config)

                logging.info("[CONNECTOR] Config refreshed from backend")
                return config

            elif response.status_code == 401:
                logging.error("[CONNECTOR] Control-plane token invalid or expired")
            else:
                logging.warning(f"[CONNECTOR] Config fetch failed: {response.status_code}")

        except Exception as e:
            logging.warning(f"[CONNECTOR] Config fetch error: {e}")

        return None

    def send_heartbeat(self, status: str = "online", error: str = None) -> None:
        """Send heartbeat to backend."""

        control_plane_token = self.secure.get_control_plane_token()
        if not control_plane_token:
            return

        payload = {
            "workstationId": self.workstation_id,
            "status": status,
            "appVersion": "1.0.0",
            "osType": platform_module.system().lower(),
            "lastError": error
        }

        try:
            requests.post(
                f"{self.api_base}?action=heartbeat",
                headers={
                    "X-API-Key": self.shared_api_key,
                    "X-Control-Plane-Token": control_plane_token,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=5
            )
        except Exception as e:
            logging.warning(f"[CONNECTOR] Heartbeat failed: {e}")

    def run_heartbeat_loop(self) -> None:
        """Background thread sending heartbeat every 30 seconds."""
        while True:
            self.send_heartbeat("online")
            time.sleep(30)

    def verify_registration(self) -> bool:
        """Verify that this device is still registered on the server.

        Returns True if registered, False if unpaired/removed.
        """
        control_plane_token = self.secure.get_control_plane_token()
        if not control_plane_token:
            return False

        try:
            response = requests.get(
                f"{self.api_base}?action=config&workstation_id={self.workstation_id}",
                headers={
                    "X-API-Key": self.shared_api_key,
                    "X-Control-Plane-Token": control_plane_token
                },
                timeout=10
            )

            # 200 = registered, 401 = not found/removed, other = error
            if response.status_code == 200:
                return True
            elif response.status_code == 401:
                logging.warning("[CONNECTOR] Device no longer registered on server")
                return False
            else:
                logging.warning(f"[CONNECTOR] Registration check failed: {response.status_code}")
                return True  # Assume OK on network errors
        except Exception as e:
            logging.warning(f"[CONNECTOR] Registration check error: {e}")
            return True  # Assume OK on network errors

    def _store_secrets(self, config: Dict[str, Any]) -> None:
        """Extract and store secrets from config to keychain."""

        # Store shared API key
        if "apiKey" in config:
            self.secure.set_shared_api_key(config["apiKey"])
        elif "edgeFunctions" in config and "apiKey" in config["edgeFunctions"]:
            self.secure.set_shared_api_key(config["edgeFunctions"]["apiKey"])

        # Store ERP auth secrets
        if "erp" in config and "auth" in config["erp"]:
            auth = config["erp"]["auth"]
            mode = auth.get("mode", "none")

            if mode == "basic":
                username = auth.get("basicUsername")
                password = auth.get("basicPassword")
                if username and password:
                    self.secure.set_erp_basic_auth(username, password)

            elif mode == "bearer":
                token = auth.get("bearerToken")
                if token:
                    self.secure.set_erp_bearer_token(token)


class SetupWizard:
    """First-run setup wizard for connector pairing."""

    def __init__(self, connector_manager: ConnectorManager):
        if not TKINTER_AVAILABLE:
            raise RuntimeError(
                "Tkinter is not available.\n\n"
                "This usually means the Tcl/Tk libraries were not bundled correctly.\n"
                "Please use console mode: qc-print-agent.exe --console"
            )

        try:
            self.root = tk.Tk()
        except Exception as e:
            raise RuntimeError(
                f"Failed to create GUI window: {e}\n\n"
                "The Tkinter library cannot initialize.\n"
                "Please use console mode: qc-print-agent.exe --console"
            )

        self.connector = connector_manager
        self.paired_successfully = False
        self.close_reason = "unknown"
        self.root.title("QC Connector Setup")
        self.root.geometry("500x400")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        self.create_widgets()

        # Ensure window is visible and on top
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after_idle(self.root.attributes, '-topmost', False)
        self.root.focus_force()
    def create_widgets(self):
        # Header
        header = ttk.Label(self.root, text="Connect to Warehouse", font=("Helvetica", 16, "bold"))
        header.pack(pady=20)

        # Instructions
        instructions = ttk.Label(
            self.root,
            text="Enter the 6-digit pairing code from the warehouse connector page:",
            wraplength=450
        )
        instructions.pack(pady=10)

        # Pairing code input
        code_frame = ttk.Frame(self.root)
        code_frame.pack(pady=20)

        ttk.Label(code_frame, text="Pairing Code:").pack(side=tk.LEFT, padx=5)

        self.code_entry = ttk.Entry(code_frame, width=15, font=("Courier", 14))
        self.code_entry.pack(side=tk.LEFT, padx=5)
        self.code_entry.focus()

        # Station name (optional)
        name_frame = ttk.Frame(self.root)
        name_frame.pack(pady=10)

        ttk.Label(name_frame, text="Station Name (optional):").pack(side=tk.LEFT, padx=5)

        self.name_entry = ttk.Entry(name_frame, width=30)
        self.name_entry.pack(side=tk.LEFT, padx=5)

        # Buttons
        button_frame = ttk.Frame(self.root)
        button_frame.pack(pady=20)

        ttk.Button(button_frame, text="Connect", command=self.on_connect, width=15).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.on_cancel, width=15).pack(side=tk.LEFT, padx=5)

        # Status
        self.status_label = ttk.Label(self.root, text="", wraplength=450)
        self.status_label.pack(pady=20)

    def on_connect(self):
        from tkinter import messagebox

        code = self.code_entry.get().strip().upper()
        station_name = self.name_entry.get().strip() or None

        if not code or len(code) != 6:
            messagebox.showerror("Error", "Please enter a valid 6-digit pairing code")
            return

        self.status_label.config(text="Connecting to warehouse...")
        self.root.update()

        result = self.connector.pair_with_code(code, station_name)

        if result.get("success"):
            station_name = result.get("stationName", "Unknown")
            self.paired_successfully = True
            self.close_reason = "success"
            messagebox.showinfo(
                "Success",
                f"Successfully paired to {station_name}!\n\n"
                f"You can now close this window. The connector will start automatically."
            )
            self.on_cancel()
        else:
            error = result.get("error", "Unknown error")
            messagebox.showerror("Error", f"Failed to pair: {error}")
            self.status_label.config(text="")

    def on_cancel(self):
        if not self.paired_successfully:
            self.close_reason = "cancel"
        self.root.destroy()

    def on_window_close(self):
        if not self.paired_successfully:
            self.close_reason = "window_close"
        self.root.destroy()

    def run(self) -> bool:
        """Start the Tkinter main loop."""
        self.root.mainloop()
        return self.paired_successfully


def console_pairing(connector_manager: ConnectorManager) -> bool:
    """Fallback console-based pairing when GUI fails.

    This provides an alternative way to pair the connector when Tkinter
    is not available or the GUI fails to display.
    """
    print("\n" + "=" * 50)
    print("  CONSOLE-BASED CONNECTOR PAIRING")
    print("=" * 50)
    print()
    print("Enter the 6-digit pairing code from the warehouse connector page:")
    print()

    try:
        code = input("Pairing Code: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        print("\nPairing cancelled.")
        return False

    if not code or len(code) != 6:
        print("\nERROR: Invalid pairing code. Please enter exactly 6 digits.")
        return False

    print()
    try:
        station_name = input("Station Name (optional, press Enter to skip): ").strip() or None
    except (EOFError, KeyboardInterrupt):
        station_name = None

    print()
    print("Connecting to warehouse...")
    result = connector_manager.pair_with_code(code, station_name)

    if result.get("success"):
        station = result.get('stationName', 'Unknown')
        print(f"\n{'=' * 50}")
        print(f"  SUCCESS!")
        print(f"{'=' * 50}")
        print(f"Paired to: {station}")
        print(f"Workstation ID: {result.get('workstationId', 'N/A')}")
        print()
        print("You can now close this window and restart the connector.")
        print(f"{'=' * 50}\n")
        return True
    else:
        error = result.get('error', 'Unknown error')
        print(f"\n{'=' * 50}")
        print(f"  PAIRING FAILED")
        print(f"{'=' * 50}")
        print(f"Error: {error}")
        print(f"{'=' * 50}\n")
        return False


class PrintAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.shutdown_requested = False  # Flag for graceful shutdown
        self.headers = {
            "X-API-Key": config.print_agent_api_key,
            "X-Workstation-Id": config.workstation_id,  # Workstation identity for tracking
            "Content-Type": "application/json",
        }

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    def _is_erp_job(self, job: Dict[str, Any]) -> bool:
        job_type = str(job.get("job_type", "")).strip().lower()
        channel = str(job.get("channel", "")).strip().lower()
        return (
            job_type == "erp"
            or channel == "erp"
            or "erp_endpoint_key" in job
            or "endpoint_key" in job
            or "endpointKey" in job
        )

    def _extract_erp_endpoint_key(self, job: Dict[str, Any]) -> str:
        raw = (
            job.get("erp_endpoint_key")
            or job.get("endpoint_key")
            or job.get("endpointKey")
            or ""
        )
        endpoint_key = str(raw).strip()
        if not endpoint_key:
            raise ValueError("ERP job missing required endpoint key")
        if endpoint_key not in (self.config.erp_endpoints or {}):
            raise ValueError(
                f"ERP endpoint key '{endpoint_key}' is not configured on this workstation"
            )
        return endpoint_key

    def _build_erp_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "X-Workstation-Id": self.config.workstation_id,
        }

        if self.config.erp_auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.config.erp_auth_bearer_token}"
        elif self.config.erp_auth_mode == "basic":
            username = self.config.erp_auth_basic_username or ""
            password = self.config.erp_auth_basic_password or ""
            credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        elif self.config.erp_auth_mode == "static_headers":
            headers.update(self.config.erp_auth_static_headers or {})

        return headers

    def _send_to_erp(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Send ERP request using local endpoint config (whitelist security model).

        Retries are performed for:
        - Network errors and timeouts
        - HTTP status 408, 429, and all 5xx responses

        4xx responses other than 408/429 are returned immediately without retry.

        Returns:
            Dict with response details including correlation_id and idempotency_key.
        """
        if not self.config.erp_enabled:
            raise ValueError("ERP job received but ERP_ENABLED is false")

        endpoint_key = self._extract_erp_endpoint_key(job)
        endpoint = (self.config.erp_endpoints or {})[endpoint_key]
        headers = self._build_erp_headers()

        # Add correlation ID and idempotency key for traceability
        correlation_id = (
            job.get("correlation_id")
            or job.get("correlationId")
            or str(uuid.uuid4())
        )
        idempotency_key = (
            job.get("idempotency_key")
            or job.get("idempotencyKey")
            or job.get("id")  # Fallback to job ID
            or str(uuid.uuid4())
        )
        headers["X-Correlation-Id"] = correlation_id
        headers["Idempotency-Key"] = idempotency_key

        # Never trust ERP URL/auth in cloud payload; only local endpoint config is used.
        payload = job.get("erp_payload")
        if payload is None:
            payload = job.get("payload")

        # Use advanced retry settings if configured, otherwise fall back to simple retry
        max_attempts = self.config.erp_retry_max_attempts
        if self.config.erp_retry_attempts > 0:
            max_attempts = max(max_attempts, self.config.erp_retry_attempts + 1)

        retryable_http_statuses = {408, 429}
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.request(
                    method=endpoint.method,
                    url=endpoint.url,
                    headers=headers,
                    json=payload if isinstance(payload, (dict, list)) else {"payload": payload},
                    timeout=min(endpoint.timeout_seconds, self.config.erp_timeout_max_seconds),
                )
                status = response.status_code
                is_retryable_http = (status in retryable_http_statuses) or (500 <= status <= 599)

                if is_retryable_http and attempt < max_attempts:
                    backoff_seconds = min(
                        self.config.erp_retry_backoff_base_seconds * (2 ** (attempt - 1)),
                        self.config.erp_retry_backoff_max_seconds,
                    )
                    sleep_seconds = backoff_seconds + random.uniform(0, self.config.erp_retry_jitter_seconds)
                    logging.warning(
                        "ERP request retryable HTTP status=%s job=%s endpoint=%s attempt=%d/%d sleeping=%.2fs",
                        status,
                        job.get("id"),
                        endpoint_key,
                        attempt,
                        max_attempts,
                        sleep_seconds,
                    )
                    time.sleep(sleep_seconds)
                    continue

                # Non-retryable 4xx (or final attempt for retryable codes)
                if 400 <= status <= 499 and status not in retryable_http_statuses:
                    logging.error("ERP request non-retryable HTTP status=%s job=%s endpoint=%s",
                                 status, job.get("id"), endpoint_key)

                response.raise_for_status()
                logging.info("ERP request succeeded job=%s endpoint=%s correlation_id=%s",
                           job.get("id"), endpoint_key, correlation_id)
                return {
                    "status_code": status,
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                    "body": response.text,
                }

            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= max_attempts:
                    raise Exception(
                        f"ERP request failed after {max_attempts} attempt(s): {exc}"
                    ) from exc
                backoff_seconds = min(
                    self.config.erp_retry_backoff_base_seconds * (2 ** (attempt - 1)),
                    self.config.erp_retry_backoff_max_seconds,
                )
                sleep_seconds = backoff_seconds + random.uniform(0, self.config.erp_retry_jitter_seconds)
                logging.warning(
                    "ERP request network error job=%s endpoint=%s attempt=%d/%d; retrying in %.2fs: %s",
                    job.get("id"),
                    endpoint_key,
                    attempt,
                    max_attempts,
                    sleep_seconds,
                    exc,
                )
                time.sleep(sleep_seconds)

    def _send_erp_http_request(
        self,
        endpoint_key: str,
        payload: Any,
        correlation_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Generic HTTP sender for ERP endpoints (reusable by both print and ERP-agent paths).

        Args:
            endpoint_key: Key from ERP_ENDPOINTS_JSON whitelist
            payload: Request payload (dict, list, or other)
            correlation_id: Correlation ID for tracing
            idempotency_key: Idempotency key for safe retries

        Returns:
            Dict with status_code, body, correlation_id, idempotency_key

        Raises:
            Exception: After all retry attempts exhausted
        """
        if not self.config.erp_enabled:
            raise ValueError("ERP request failed but ERP_ENABLED is false")

        if endpoint_key not in (self.config.erp_endpoints or {}):
            raise ValueError(
                f"ERP endpoint key '{endpoint_key}' is not configured on this workstation"
            )

        endpoint = (self.config.erp_endpoints or {})[endpoint_key]
        headers = self._build_erp_headers()
        headers["X-Correlation-Id"] = correlation_id
        headers["Idempotency-Key"] = idempotency_key

        # Use advanced retry settings if configured, otherwise fall back to simple retry
        max_attempts = self.config.erp_retry_max_attempts
        if self.config.erp_retry_attempts > 0:
            max_attempts = max(max_attempts, self.config.erp_retry_attempts + 1)

        retryable_http_statuses = {408, 429}
        for attempt in range(1, max_attempts + 1):
            try:
                # Log the outgoing request details
                logging.info(
                    "[ERP-AGENT] Sending ERP request: method=%s url=%s correlation_id=%s idempotency_key=%s",
                    endpoint.method,
                    endpoint.url,
                    correlation_id,
                    idempotency_key,
                )
                # Log payload (sanitized)
                if isinstance(payload, dict):
                    sanitized_payload = {k: v for k, v in payload.items() if "password" not in k.lower() and "token" not in k.lower()}
                    logging.info(
                        "[ERP-AGENT] Request payload:\n%s",
                        ErpAgentClient._format_json_for_log(sanitized_payload),
                    )
                else:
                    payload_preview = ErpAgentClient._format_json_for_log(payload)
                    if len(payload_preview) > 500:
                        payload_preview = f"{payload_preview[:500]}..."
                    logging.info("[ERP-AGENT] Request payload:\n%s", payload_preview)

                # Always send JSON BODY (even for GET) - 1C requires this for ReadJSON()
                request_kwargs = {
                    "method": endpoint.method,
                    "url": endpoint.url,
                    "headers": {
                        **headers,
                        "Content-Type": "application/json",
                    },
                    "timeout": min(endpoint.timeout_seconds, self.config.erp_timeout_max_seconds),
                }

                try:
                    json.dumps(payload)
                except Exception as exc:
                    raise ValueError(f"Invalid JSON payload: {exc}") from exc

                if isinstance(payload, (dict, list)):
                    request_kwargs["data"] = json.dumps(payload, ensure_ascii=False)
                else:
                    request_kwargs["data"] = json.dumps({"value": payload}, ensure_ascii=False)

                logging.info(
                    "[ERP-AGENT] Final request body:\n%s",
                    ErpAgentClient._format_json_for_log(request_kwargs["data"]),
                )

                response = requests.request(**request_kwargs)
                status = response.status_code
                is_retryable_http = (status in retryable_http_statuses) or (500 <= status <= 599)

                # Log response details
                logging.info(
                    "[ERP-AGENT] ERP response: status=%s correlation_id=%s response_preview=%s",
                    status,
                    correlation_id,
                    response.text[:200] if response.text else "(empty)",
                )

                if is_retryable_http and attempt < max_attempts:
                    backoff_seconds = min(
                        self.config.erp_retry_backoff_base_seconds * (2 ** (attempt - 1)),
                        self.config.erp_retry_backoff_max_seconds,
                    )
                    sleep_seconds = backoff_seconds + random.uniform(0, self.config.erp_retry_jitter_seconds)
                    logging.warning(
                        "[ERP-AGENT] Retryable HTTP status=%s endpoint=%s attempt=%d/%d sleeping=%.2fs",
                        status,
                        endpoint_key,
                        attempt,
                        max_attempts,
                        sleep_seconds,
                    )
                    time.sleep(sleep_seconds)
                    continue

                # Non-retryable 4xx (or final attempt for retryable codes)
                if 400 <= status <= 499 and status not in retryable_http_statuses:
                    logging.error("[ERP-AGENT] Non-retryable HTTP status=%s endpoint=%s",
                                 status, endpoint_key)

                response.raise_for_status()
                logging.info("[ERP-AGENT] ERP request succeeded endpoint=%s correlation_id=%s",
                           endpoint_key, correlation_id)
                return {
                    "status_code": status,
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                    "body": response.text,
                }

            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= max_attempts:
                    logging.error(
                        "[ERP-AGENT] ERP request failed after %d attempts: method=%s url=%s correlation_id=%s error=%s",
                        max_attempts,
                        endpoint.method,
                        endpoint.url,
                        correlation_id,
                        exc,
                    )
                    raise Exception(
                        f"ERP request failed after {max_attempts} attempt(s): {exc}"
                    ) from exc
                backoff_seconds = min(
                    self.config.erp_retry_backoff_base_seconds * (2 ** (attempt - 1)),
                    self.config.erp_retry_backoff_max_seconds,
                )
                sleep_seconds = backoff_seconds + random.uniform(0, self.config.erp_retry_jitter_seconds)
                logging.warning(
                    "[ERP-AGENT] Network error endpoint=%s attempt=%d/%d; retrying in %.2fs: method=%s url=%s error=%s",
                    endpoint_key,
                    attempt,
                    max_attempts,
                    sleep_seconds,
                    endpoint.method,
                    endpoint.url,
                    exc,
                )
                time.sleep(sleep_seconds)

    def _process_erp_box_fetch(self, request: GenericErpRequest, client: ErpAgentClient) -> bool:
        """Process a single ERP box fetch request.

        Args:
            request: Generic ERP request with business_type='erp_box_fetch'
            client: ErpAgentClient for callbacks

        Returns:
            True if successful, False otherwise
        """
        logging.info("[ERP-AGENT] Processing business_type=erp_box_fetch request_id=%s invoice_id=%s quantity=%s",
                   request.request_id, request.invoice_id, request.quantity)

        # Generate correlation ID and idempotency key
        correlation_id = str(uuid.uuid4())
        idempotency_key = request.request_id

        try:
            if request.request_payload is None:
                raise ValueError("ERP request failed: request_payload is missing or empty")
            if not isinstance(request.request_payload, dict):
                raise ValueError(
                    f"ERP request failed: request_payload must be a JSON object, got {type(request.request_payload)}"
                )

            # Forward the request payload with only the current fixed-warehouse override.
            erp_payload = dict(request.request_payload)
            erp_payload["warehouse_id"] = "000093"

            # Debug logging - ERP BOX FETCH REQUEST
            logging.info("=" * 60)
            logging.info("[ERP-AGENT] === BOX FETCH REQUEST TO 1C ===")
            logging.info("[ERP-AGENT] endpoint_key: erp_box_fetch")
            logging.info("[ERP-AGENT] request_id: %s", request.request_id)
            logging.info("[ERP-AGENT] correlation_id: %s", correlation_id)
            logging.info(
                "[ERP-AGENT] PAYLOAD:\n%s",
                ErpAgentClient._format_json_for_log(erp_payload),
            )
            logging.info("=" * 60)

            # Call local ERP endpoint
            response = self._send_erp_http_request(
                endpoint_key="erp_box_fetch",
                payload=erp_payload,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )

            # Parse and normalize ERP response
            try:
                # 1C may return JSON with UTF-8 BOM - strip it before parsing
                body = response["body"]
                if body.startswith('\ufeff'):
                    logging.debug("[ERP-AGENT] Stripping UTF-8 BOM from 1C response")
                    body = body[1:]  # Strip BOM
                response_body = json.loads(body, strict=False)
            except json.JSONDecodeError as exc:
                logging.error("[ERP-AGENT] JSON decode error: %s, body=%s", exc, response["body"][:200])
                response_body = {"raw": response["body"]}

            # Validate ERP response format
            if not isinstance(response_body, dict):
                raise ValueError(f"ERP response is not a dict: {type(response_body)}")

            if response_body.get("status") != "success":
                raise ValueError(f"ERP returned status: {response_body.get('status')}")

            boxes = response_body.get("boxes")
            if not isinstance(boxes, list):
                raise ValueError(f"ERP boxes is not a list: {type(boxes)}")

            # Normalize boxes to expected format
            normalized_boxes = []
            for box in boxes:
                if not isinstance(box, dict):
                    continue
                normalized_boxes.append({
                    "box_id": str(box.get("box_id", "")),
                    "sscc": box.get("sscc"),
                    "dm_code": box.get("dm_code"),
                })

            # Log 1C response
            logging.info("=" * 60)
            logging.info("[ERP-AGENT] === BOX FETCH RESPONSE FROM 1C ===")
            logging.info("[ERP-AGENT] request_id: %s", request.request_id)
            logging.info("[ERP-AGENT] status: success")
            logging.info("[ERP-AGENT] boxes_count: %d", len(normalized_boxes))
            logging.info(
                "[ERP-AGENT] boxes:\n%s",
                ErpAgentClient._format_json_for_log(normalized_boxes),
            )
            logging.info("=" * 60)
            logging.info("")

            # Callback success
            client.post_success(request.request_id, normalized_boxes)
            return True

        except Exception as exc:
            error_msg = str(exc)
            logging.error("[ERP-AGENT] Failed request_id=%s: %s", request.request_id, error_msg)

            # Callback failure
            try:
                client.post_failure(request.request_id, error_msg)
            except Exception as callback_exc:
                logging.error("[ERP-AGENT] Failed to send failure callback for request_id=%s: %s",
                            request.request_id, callback_exc)

            return False

    def _route_erp_request(self, request: GenericErpRequest, client: ErpAgentClient) -> bool:
        """Route ERP request to appropriate handler based on business_type.

        Args:
            request: Generic ERP request
            client: ErpAgentClient for callbacks

        Returns:
            True if successful, False otherwise
        """
        # Log incoming request for debugging
        logging.info("")
        logging.info(">>> [ERP-AGENT] INCOMING REQUEST ROUTER <<<")
        logging.info("    business_type: %s", request.business_type)
        logging.info("    request_id: %s", request.request_id)
        logging.info("    endpoint_key: %s", request.endpoint_key)
        logging.info("    attempts: %d/%d", request.attempts, request.max_attempts)
        logging.info(
            "    request_payload:\n%s",
            ErpAgentClient._format_json_for_log(request.request_payload),
        )
        logging.info("")

        if request.business_type == "erp_box_fetch":
            return self._process_erp_box_fetch(request, client)
        elif request.business_type == "completion_event":
            return self._process_completion_event(request, client)
        else:
            logging.error("[ERP-AGENT] Unknown business_type=%s request_id=%s",
                         request.business_type, request.request_id)
            try:
                client.post_failure(request.request_id, f"Unknown business_type: {request.business_type}")
            except Exception as callback_exc:
                logging.error("[ERP-AGENT] Failed to send failure callback for request_id=%s: %s",
                            request.request_id, callback_exc)
            return False

    def _process_completion_event(self, request: GenericErpRequest, client: ErpAgentClient) -> bool:
        """Process completion event request to 1C.

        IMPORTANT: request_payload is passed through exactly as received from Supabase.
        No timestamp normalization or field modifications for completion events.

        Args:
            request: Generic ERP request with business_type='completion_event'
            client: ErpAgentClient for callbacks

        Returns:
            True if successful, False otherwise
        """
        logging.info("[ERP-AGENT] Processing business_type=completion_event request_id=%s message_id=%s",
                   request.request_id, request.request_payload.get("message_id") if request.request_payload else "N/A")

        # Validate required fields exist in request_payload
        required_fields = ["message_id", "invoice_id", "client_id", "timestamp",
                          "operator_id", "warehouse_id", "boxes", "shortages", "summary"]

        if not request.request_payload:
            error_msg = "ERP request failed: request_payload is missing or empty"
            logging.error("[ERP-AGENT] %s request_id=%s", error_msg, request.request_id)
            try:
                client.post_completion_failure(request.request_id, error_msg)
            except Exception as callback_exc:
                logging.error("[ERP-AGENT] Failed to send failure callback for request_id=%s: %s",
                            request.request_id, callback_exc)
            return False

        for field in required_fields:
            if field not in request.request_payload:
                error_msg = f"ERP request failed: required field '{field}' is missing from request_payload"
                logging.error("[ERP-AGENT] %s request_id=%s", error_msg, request.request_id)
                try:
                    client.post_completion_failure(request.request_id, error_msg)
                except Exception as callback_exc:
                    logging.error("[ERP-AGENT] Failed to send failure callback for request_id=%s: %s",
                                request.request_id, callback_exc)
                return False

        # Generate correlation ID and idempotency key
        correlation_id = str(uuid.uuid4())
        idempotency_key = request.request_id

        # Use request_payload exactly as received (no modifications)
        erp_payload = request.request_payload

        # Debug logging - COMPLETION EVENT REQUEST
        logging.info("=" * 60)
        logging.info("[ERP-AGENT] === COMPLETION EVENT REQUEST TO 1C ===")
        logging.info("[ERP-AGENT] endpoint_key: completion_event")
        logging.info("[ERP-AGENT] request_id: %s", request.request_id)
        logging.info("[ERP-AGENT] correlation_id: %s", correlation_id)
        logging.info(
            "[ERP-AGENT] PAYLOAD:\n%s",
            ErpAgentClient._format_json_for_log(erp_payload),
        )
        logging.info("=" * 60)

        try:
            # Call completion_event endpoint
            response = self._send_erp_http_request(
                endpoint_key="completion_event",
                payload=erp_payload,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )

            # Parse and normalize ERP response
            try:
                # 1C may return JSON with UTF-8 BOM - strip it before parsing
                body = response["body"]
                if body.startswith('\ufeff'):
                    logging.debug("[ERP-AGENT] Stripping UTF-8 BOM from 1C response")
                    body = body[1:]  # Strip BOM
                response_body = json.loads(body, strict=False)
            except json.JSONDecodeError as exc:
                logging.error("[ERP-AGENT] JSON decode error: %s, body=%s", exc, response["body"][:200])
                response_body = {"raw": response["body"]}

            # Validate ERP response format
            if not isinstance(response_body, dict):
                raise ValueError(f"ERP response is not a dict: {type(response_body)}")

            if response_body.get("status") != "success":
                raise ValueError(f"ERP returned status: {response_body.get('status')}")

            # Log 1C response
            logging.info("=" * 60)
            logging.info("[ERP-AGENT] === COMPLETION EVENT RESPONSE FROM 1C ===")
            logging.info("[ERP-AGENT] request_id: %s", request.request_id)
            logging.info("[ERP-AGENT] status: success")
            logging.info(
                "[ERP-AGENT] response:\n%s",
                ErpAgentClient._format_json_for_log(response_body),
            )
            logging.info("=" * 60)
            logging.info("")

            # Callback success with ERP response
            client.post_completion_success(request.request_id, response_body)
            return True

        except Exception as exc:
            error_msg = str(exc)
            logging.error("[ERP-AGENT] Failed request_id=%s: %s", request.request_id, error_msg)

            # Callback failure
            try:
                client.post_completion_failure(request.request_id, error_msg)
            except Exception as callback_exc:
                logging.error("[ERP-AGENT] Failed to send failure callback for request_id=%s: %s",
                            request.request_id, callback_exc)

            return False

    def run_erp_forever(self) -> None:
        """Run ERP-agent polling loop (separate from print loop)."""
        if not self.config.erp_agent_enabled:
            logging.warning("[ERP-AGENT] ERP-agent loop disabled (ERP_AGENT_ENABLED=false)")
            return

        if not self.config.erp_agent_url:
            logging.error("[ERP-AGENT] ERP_AGENT_URL not configured, disabling ERP-agent loop")
            return

        logging.info("[ERP-AGENT] Starting ERP-agent loop (poll_interval=%s, max_concurrent=%s)",
                   self.config.erp_agent_poll_interval_seconds,
                   self.config.erp_agent_max_concurrent_requests)

        client = ErpAgentClient(
            url=self.config.erp_agent_url,
            api_key=self.config.erp_agent_api_key or self.config.print_agent_api_key,
            workstation_id=self.config.workstation_id,
        )

        with ThreadPoolExecutor(max_workers=self.config.erp_agent_max_concurrent_requests,
                               thread_name_prefix="ErpAgentWorker") as executor:
            while not self.shutdown_requested:
                try:
                    requests = client.poll_requests(limit=self.config.erp_agent_max_concurrent_requests)
                    if not requests:
                        # Sleep with interrupt check
                        for _ in range(int(self.config.erp_agent_poll_interval_seconds * 10)):
                            if self.shutdown_requested:
                                break
                            time.sleep(0.1)
                        if self.shutdown_requested:
                            break
                        continue

                    logging.info("[ERP-AGENT] Processing %d requests", len(requests))

                    # Submit all requests to thread pool (routed by business_type)
                    futures = {
                        executor.submit(self._route_erp_request, req, client): req
                        for req in requests
                    }

                    # Wait for all requests to complete
                    for future in as_completed(futures, timeout=300):
                        req = futures[future]
                        try:
                            result = future.result()
                            if not result:
                                logging.warning("[ERP-AGENT] Request %s failed during processing",
                                              req.request_id)
                        except Exception as exc:
                            logging.exception("[ERP-AGENT] Unexpected error processing request %s: %s",
                                            req.request_id, exc)

                except Exception as exc:
                    logging.exception("[ERP-AGENT] Loop error: %s", exc)
                    if not self.shutdown_requested:
                        time.sleep(self.config.erp_agent_poll_interval_seconds)

        logging.info("[ERP-AGENT] ERP-agent loop stopped")

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            logging.info("Received signal %s, initiating graceful shutdown...", signum)
            self.shutdown_requested = True

        # Handle SIGTERM (kill), SIGINT (Ctrl+C)
        signal_module.signal(SIGTERM, signal_handler)
        signal_module.signal(SIGINT, signal_handler)

        # Handle SIGHUP (terminal close) on Unix/Linux only
        if SIGHUP is not None:
            signal_module.signal(SIGHUP, signal_handler)

    def run_forever(self) -> None:
        logging.info("print-agent started (max_concurrent_jobs=%d)", self.config.max_concurrent_jobs)
        logging.info("Press Ctrl+C to stop (agent will finish current jobs first)")

        with ThreadPoolExecutor(max_workers=self.config.max_concurrent_jobs,
                               thread_name_prefix="PrinterWorker") as executor:
            while not self.shutdown_requested:
                try:
                    jobs = self._fetch_pending_jobs()
                    if not jobs:
                        # Sleep with interrupt check
                        for _ in range(int(self.config.poll_interval_seconds * 10)):
                            if self.shutdown_requested:
                                break
                            time.sleep(0.1)
                        if self.shutdown_requested:
                            break
                        continue

                    logging.info("Fetched %d jobs, submitting to thread pool", len(jobs))

                    # Submit all jobs to thread pool
                    futures = {
                        executor.submit(self.process_job, job): job
                        for job in jobs
                    }

                    # Wait for all jobs to complete
                    try:
                        for future in as_completed(futures, timeout=300):
                            job = futures[future]
                            try:
                                result = future.result()
                                if not result:
                                    logging.warning("Job %s failed during processing", job.get("id"))
                            except Exception as exc:
                                logging.exception("Unexpected error processing job %s: %s",
                                                job.get("id"), exc)
                                # Mark job as failed in backend
                                try:
                                    self._mark_failed(job, str(exc))
                                except Exception as callback_exc:
                                    logging.error("Failed to mark job %s as failed: %s",
                                                 job.get("id"), callback_exc)
                    except TimeoutError:
                        logging.error("Job processing timeout after 300 seconds - cancelling remaining jobs")
                        # Cancel any futures still running
                        for future in futures:
                            if not future.done():
                                future.cancel()
                                logging.warning("Cancelled job for future %s", future)
                        # Don't crash - just log and continue to next iteration

                except Exception as exc:  # broad by design for resilient loop
                    logging.exception("loop error: %s", exc)
                    if not self.shutdown_requested:
                        time.sleep(self.config.poll_interval_seconds)

        # Graceful shutdown complete
        logging.info("Print agent shutdown complete")

    def _fetch_pending_jobs(self) -> List[Dict[str, Any]]:
        """Fetch multiple pending jobs up to max_concurrent_jobs limit."""
        limit = self.config.max_concurrent_jobs
        try:
            response = requests.get(
                self.config.print_agent_url,
                headers=self.headers,
                params={"action": "poll", "limit": str(limit)},
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except requests.ConnectionError as exc:
            logging.warning("Connection error: %s - will retry", exc)
            return []  # Return empty list, will retry
        except requests.Timeout as exc:
            logging.warning("Request timeout: %s - will retry", exc)
            return []
        except requests.HTTPError as exc:
            logging.error("HTTP error fetching jobs: %s", exc)
            return []
        except ValueError as exc:
            logging.error("Invalid JSON response: %s", exc)
            return []

        jobs: List[Dict[str, Any]]
        if isinstance(body, dict):
            jobs = body.get("jobs") or []
        elif isinstance(body, list):
            jobs = body
        else:
            logging.error("Unexpected poll response format: %s", type(body))
            return []

        # Defensive check: filter out jobs that have exceeded max_attempts
        # This prevents infinite retry loops when backend doesn't properly filter them
        filtered_jobs = []
        for job in jobs:
            try:
                attempt = int(job.get("attempt", 0))
                max_attempts = int(job.get("max_attempts", 3))
                if attempt >= max_attempts:
                    logging.warning(
                        "Skipping job_id=%s that has exceeded max_attempts (attempt=%d, max_attempts=%d). "
                        "Backend should have filtered this out - marking as failed.",
                        job.get("id"), attempt, max_attempts
                    )
                    # Notify backend to mark as failed (defensive measure)
                    try:
                        self._mark_failed(job, f"Exceeded max_attempts ({attempt}/{max_attempts})")
                    except Exception as exc:
                        logging.error("Failed to mark job %s as failed: %s", job.get("id"), exc)
                    continue
                filtered_jobs.append(job)
            except (ValueError, TypeError) as exc:
                logging.error("Invalid job format for attempt/max_attempts check: %s", exc)
                filtered_jobs.append(job)  # Include job anyway to avoid losing it

        return filtered_jobs

    def _fetch_single_job(self) -> Optional[Dict[str, Any]]:
        """Fetch a single pending job (backwards compatible)."""
        try:
            response = requests.get(
                self.config.print_agent_url,
                headers=self.headers,
                params={"action": "poll", "limit": "1"},
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except requests.ConnectionError as exc:
            logging.warning("Connection error: %s - will retry", exc)
            return None
        except requests.Timeout as exc:
            logging.warning("Request timeout: %s - will retry", exc)
            return None
        except requests.HTTPError as exc:
            logging.error("HTTP error fetching job: %s", exc)
            return None
        except ValueError as exc:
            logging.error("Invalid JSON response: %s", exc)
            return None

        jobs: List[Dict[str, Any]]
        if isinstance(body, dict):
            jobs = body.get("jobs") or []
        elif isinstance(body, list):
            jobs = body
        else:
            logging.error("Unexpected poll response format: %s", type(body))
            return None

        # Defensive check: filter out jobs that have exceeded max_attempts
        for job in jobs:
            try:
                attempt = int(job.get("attempt", 0))
                max_attempts = int(job.get("max_attempts", 3))
                if attempt >= max_attempts:
                    logging.warning(
                        "Skipping job_id=%s that has exceeded max_attempts (attempt=%d, max_attempts=%d). "
                        "Backend should have filtered this out - marking as failed.",
                        job.get("id"), attempt, max_attempts
                    )
                    try:
                        self._mark_failed(job, f"Exceeded max_attempts ({attempt}/{max_attempts})")
                    except Exception as exc:
                        logging.error("Failed to mark job %s as failed: %s", job.get("id"), exc)
                    continue
                # Return first valid job
                return job
            except (ValueError, TypeError) as exc:
                logging.error("Invalid job format for attempt/max_attempts check: %s", exc)
                return job  # Return job anyway to avoid losing it
        return None

    def _send_to_printer(self, job: Dict[str, Any]) -> None:
        """Send ZPL data to printer with guaranteed socket cleanup."""
        printer_ip = job.get("printer_ip")
        if not printer_ip or not isinstance(printer_ip, str):
            raise ValueError("job missing valid printer_ip string")

        printer_port_raw = job.get("printer_port", self.config.printer_port)
        try:
            printer_port = int(printer_port_raw)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid printer_port value: {printer_port_raw}")

        if not (1 <= printer_port <= 65535):
            raise ValueError(f"printer_port out of valid range (1-65535): {printer_port}")

        zpl = job.get("zpl_data")
        if not zpl or not isinstance(zpl, str):
            raise ValueError("job missing valid zpl_data string")
        if not zpl.strip():
            raise ValueError("zpl_data is empty or whitespace only")
        zpl_bytes = zpl.encode("utf-8")
        payload_hash = hashlib.sha256(zpl_bytes).hexdigest()
        logging.debug(
            "job=%s payload metadata size_bytes=%d sha256=%s message_id=%s correlation_id=%s",
            job.get("id"),
            len(zpl_bytes),
            payload_hash,
            job.get("message_id"),
            job.get("correlation_id"),
        )

        try:
            # Use context manager for guaranteed socket cleanup
            with socket.create_connection(
                (printer_ip, printer_port),
                timeout=self.config.printer_timeout_seconds,
            ) as sock:
                sock.sendall(zpl_bytes)
            logging.info("Label sent to printer at %s:%s", printer_ip, printer_port)
        except socket.timeout:
            raise Exception(f"Connection timeout to printer {printer_ip}:{printer_port}")
        except ConnectionRefusedError:
            raise Exception(f"Connection refused by printer {printer_ip}:{printer_port}")
        except OSError as e:
            raise Exception(f"Network error communicating with printer {printer_ip}:{printer_port}: {e}")

    def _sanitize_payload_text(self, value: Any, max_length: int = 2000) -> Optional[str]:
        if value is None:
            return None
        text = str(value)
        lowered = text.lower()
        for marker in ("authorization:", "api-key", "x-api-key", "token", "bearer "):
            if marker in lowered:
                return "[redacted]"
        if len(text) > max_length:
            return f"{text[:max_length]}... [truncated]"
        return text

    def _parse_optional_int(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _extract_job_field(self, job: Dict[str, Any], *keys: str) -> Any:
        metadata = job.get("metadata")
        for key in keys:
            if key in job and job.get(key) is not None:
                return job.get(key)
            if isinstance(metadata, dict) and key in metadata and metadata.get(key) is not None:
                return metadata.get(key)
        return None

    def _extract_job_context(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "channel": self._extract_job_field(job, "channel") or "print",
            "attempt": self._parse_optional_int(self._extract_job_field(job, "attempt")),
            "max_attempts": self._parse_optional_int(
                self._extract_job_field(job, "maxAttempts", "max_attempts")
            ),
            "endpoint_key": self._extract_job_field(job, "endpointKey", "endpoint_key"),
            "business_type": self._extract_job_field(job, "businessType", "business_type"),
            "business_id": self._extract_job_field(job, "businessId", "business_id"),
            "correlation_id": self._extract_job_field(job, "correlationId", "correlation_id"),
            "erp_body": self._extract_job_field(
                job,
                "erpBody",
                "erp_body",
                "erpResponseBody",
                "erp_response_body",
                "responseBody",
                "response_body",
            ),
        }

    def _prepare_erp_body(self, erp_body: Any) -> Any:
        if erp_body is None:
            return None
        if isinstance(erp_body, (dict, list)):
            return erp_body
        return self._sanitize_payload_text(erp_body)

    def _classify_error(self, error_message: str) -> str:
        lowered = error_message.lower()
        if "timeout" in lowered:
            return "printer_timeout"
        if "refused" in lowered:
            return "connection_refused"
        if "missing valid" in lowered or "out of valid range" in lowered or "empty" in lowered:
            return "invalid_job_payload"
        return "printer_transport_error"

    def _failure_status_for_attempt(self, attempt: Optional[int], max_attempts: Optional[int]) -> str:
        if attempt is not None and max_attempts is not None and attempt < max_attempts:
            return "retry_scheduled"
        return "failed"

    def _mark_done(
        self,
        job: Dict[str, Any],
        duration_ms: Optional[int] = None,
        context_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        context = self._extract_job_context(job)
        if context_overrides:
            context.update(context_overrides)
        self._notify_print_service(
            job_id=job.get("id"),
            status="completed",
            error_message=None,
            channel=context["channel"],
            attempt=context["attempt"],
            max_attempts=context["max_attempts"],
            endpoint_key=context["endpoint_key"],
            business_type=context["business_type"],
            business_id=context["business_id"],
            correlation_id=context["correlation_id"],
            response_code=context.get("response_code", 200),
            retryable=False,
            error_code=None,
            transport_error_message=None,
            duration_ms=duration_ms,
            erp_body=context["erp_body"],
        )

    def _mark_failed(self, job: Dict[str, Any], error_message: str, duration_ms: Optional[int] = None) -> None:
        context = self._extract_job_context(job)
        attempt = context["attempt"]
        max_attempts = context["max_attempts"]
        status = self._failure_status_for_attempt(attempt, max_attempts)
        sanitized_error = self._sanitize_payload_text(error_message, max_length=500)
        retryable = status == "retry_scheduled"
        self._notify_print_service(
            job_id=job.get("id"),
            status=status,
            error_message=sanitized_error,
            channel=context["channel"],
            attempt=attempt,
            max_attempts=max_attempts,
            endpoint_key=context["endpoint_key"],
            business_type=context["business_type"],
            business_id=context["business_id"],
            correlation_id=context["correlation_id"],
            response_code=429 if retryable else 500,
            retryable=retryable,
            error_code=self._classify_error(error_message),
            transport_error_message=sanitized_error,
            duration_ms=duration_ms,
            erp_body=context["erp_body"],
        )

    def _notify_print_service(
        self,
        job_id: Optional[str],
        status: str,
        error_message: Optional[str],
        channel: Optional[str] = None,
        attempt: Optional[int] = None,
        max_attempts: Optional[int] = None,
        endpoint_key: Optional[str] = None,
        business_type: Optional[str] = None,
        business_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        response_code: Optional[int] = None,
        retryable: Optional[bool] = None,
        error_code: Optional[str] = None,
        transport_error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
        erp_body: Any = None,
    ) -> None:
        payload = {
            # Compatibility fields
            "jobId": job_id,
            "status": status,
            "errorMessage": error_message,
            # Extended callback context
            "channel": channel,
            "attempt": attempt,
            "maxAttempts": max_attempts,
            "endpointKey": endpoint_key,
            "businessType": business_type,
            "businessId": business_id,
            "correlationId": correlation_id,
            # Transport result fields
            "responseCode": response_code,
            "retryable": retryable,
            "errorCode": error_code,
            "durationMs": duration_ms,
            # Keep both compatibility and transport-level error detail
            "transportErrorMessage": transport_error_message,
            # Parsed ERP response body (truncated/redacted as needed)
            "erpBody": self._prepare_erp_body(erp_body),
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        callback_headers = dict(self.headers)
        callback_headers["Content-Length"] = str(len(payload_bytes))
        try:
            for attempt in range(1, DEFAULT_CALLBACK_RETRIES + 1):
                try:
                    response = requests.post(
                        self.config.print_agent_url,
                        headers=callback_headers,
                        data=payload_bytes,
                        timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    return
                except (requests.ConnectionError, requests.Timeout) as exc:
                    if attempt == DEFAULT_CALLBACK_RETRIES:
                        raise
                    logging.warning(
                        "callback retrying job=%s status=%s attempt=%d/%d size_bytes=%d sha256=%s error=%s",
                        job_id,
                        status,
                        attempt,
                        DEFAULT_CALLBACK_RETRIES,
                        len(payload_bytes),
                        payload_hash,
                        exc,
                    )
                    time.sleep(0.5 * attempt)
        except requests.ConnectionError as exc:
            logging.error("Connection error sending callback for job %s: %s", job_id, exc)
            # Don't raise - agent will continue processing other jobs
        except requests.Timeout as exc:
            logging.error("Timeout sending callback for job %s: %s", job_id, exc)
        except requests.HTTPError as exc:
            logging.error("HTTP error sending callback for job %s: %s", job_id, exc)
        except Exception as exc:
            logging.error("Unexpected error sending callback for job %s: %s", job_id, exc)

    def process_job(self, job: Dict[str, Any]) -> bool:
        """Process a single print job (thread-safe)."""
        job_id = job.get("id")
        if not job_id:
            raise ValueError(f"Job missing required 'id' field: {job}")
        channel = job.get("channel", "printer")
        thread_id = threading.current_thread().name
        logging.info("[%s] processing job=%s printer_ip=%s printer_port=%s",
                     thread_id, job_id, job.get("printer_ip"), job.get("printer_port"))
        started_at = time.monotonic()
        success_context: Optional[Dict[str, Any]] = None
        try:
            if self._is_erp_job(job):
                erp_result = self._send_to_erp(job)
                success_context = {
                    "correlation_id": erp_result.get("correlation_id"),
                    "erp_body": erp_result.get("body"),
                    "response_code": erp_result.get("status_code", 200),
                }
            else:
                self._send_to_printer(job)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            self._mark_failed(job, str(exc), duration_ms=duration_ms)
            logging.error("[%s] failed job=%s error=%s", thread_id, job_id, exc)
            return False

        duration_ms = int((time.monotonic() - started_at) * 1000)
        try:
            self._mark_done(job, duration_ms=duration_ms, context_overrides=success_context)
        except Exception as exc:
            logging.error(
                "[%s] completion callback failed after successful print job=%s error=%s",
                thread_id,
                job_id,
                exc,
            )
            return False

        logging.info("[%s] completed job=%s", thread_id, job_id)
        return True

    def process_one(self) -> bool:
        """Backwards-compatible wrapper for single job processing.

        This method is maintained for backwards compatibility with external
        callers or tests. It fetches and processes exactly one job at a time.

        For production use with multiple printers, use run_forever() which
        processes jobs in parallel using process_job().
        """
        job = self._fetch_single_job()
        if not job:
            return False
        return self.process_job(job)

    def test_printer_connection(self, printer_ip: str, port: int = None) -> Dict[str, Any]:
        """Test TCP connection to printer."""
        try:
            port = port or self.config.printer_port
            with socket.create_connection((printer_ip, port), timeout=5):
                return {"success": True, "message": f"Connected to {printer_ip}:{port}"}
        except socket.timeout:
            return {"success": False, "error": "Connection timeout"}
        except ConnectionRefusedError:
            return {"success": False, "error": "Connection refused"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def test_erp_connection(self, endpoint_key: str, test_payload: Dict[str, Any] = None) -> Dict[str, Any]:
        """Test HTTP connection to ERP endpoint."""

        if endpoint_key not in self.config.erp_endpoints:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_key}"}

        endpoint = self.config.erp_endpoints[endpoint_key]
        test_payload = test_payload or {"test": True}

        try:
            # Use actual current ERP helper signature
            response = self._send_erp_http_request(
                endpoint_key=endpoint_key,
                payload=test_payload,
                correlation_id=str(uuid.uuid4()),
                idempotency_key=str(uuid.uuid4()),
            )

            if response.get("status_code") == 200:
                return {"success": True, "message": f"Connected to {endpoint.url}"}
            else:
                return {"success": False, "error": f"HTTP {response.get('status_code')}"}

        except requests.exceptions.Timeout:
            return {"success": False, "error": "Connection timeout"}
        except requests.exceptions.ConnectionError:
            return {"success": False, "error": "Connection refused"}
        except Exception as e:
            return {"success": False, "error": str(e)}


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file.

    Searches in multiple locations if path doesn't exist:
    1. Provided path
    2. Exe directory (for bundled apps)
    3. Current working directory
    """
    from pathlib import Path

    # If explicit path provided and exists, use it
    env_path = Path(path)
    if env_path.exists():
        _load_env_file(env_path)
        return

    # Search for .env in exe directory and current directory
    search_paths = []
    if getattr(sys, 'frozen', False):
        # Running as bundled exe - check exe directory
        search_paths.append(Path(sys.executable).parent / '.env')
        search_paths.append(Path(sys.executable).parent / '_internal' / '.env')
    # Check current working directory
    search_paths.append(Path.cwd() / '.env')

    for search_path in search_paths:
        if search_path.exists():
            _load_env_file(search_path)
            return


def _load_env_file(path: Path) -> None:
    """Internal: Load environment variables from a specific .env file."""
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cleaned_key = key.strip()
            cleaned_value = value.strip().strip('"').strip("'")
            logging.info(
                "Loading env var from .env: %s=%s",
                cleaned_key,
                _sanitize_env_value_for_log(cleaned_key, cleaned_value),
            )
            os.environ[cleaned_key] = cleaned_value

# https://wktfsmiclvyhjpkibgis.supabase.co/functions/v1/print-agent

def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value (true/false), got: {value}")


def parse_erp_endpoints(raw_json: str, default_timeout_seconds: float) -> Dict[str, ErpEndpointConfig]:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ERP_ENDPOINTS_JSON must be valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("ERP_ENDPOINTS_JSON must be a JSON object keyed by endpoint key")

    endpoints: Dict[str, ErpEndpointConfig] = {}
    for endpoint_key, endpoint_value in parsed.items():
        if not isinstance(endpoint_key, str) or not endpoint_key.strip():
            raise ValueError("ERP_ENDPOINTS_JSON endpoint keys must be non-empty strings")
        if not isinstance(endpoint_value, dict):
            raise ValueError(f"ERP endpoint '{endpoint_key}' must be an object")

        url = str(endpoint_value.get("url", "")).strip()
        if not url:
            raise ValueError(f"ERP endpoint '{endpoint_key}' is missing required 'url'")

        method = str(endpoint_value.get("method", "POST")).strip().upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(
                f"ERP endpoint '{endpoint_key}' has unsupported method '{method}'"
            )

        timeout_raw = endpoint_value.get("timeout_seconds", default_timeout_seconds)
        try:
            timeout_seconds = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ERP endpoint '{endpoint_key}' timeout_seconds must be numeric, got: {timeout_raw}"
            ) from exc
        if timeout_seconds <= 0:
            raise ValueError(
                f"ERP endpoint '{endpoint_key}' timeout_seconds must be > 0, got: {timeout_seconds}"
            )

        endpoints[endpoint_key.strip()] = ErpEndpointConfig(
            url=url,
            method=method,
            timeout_seconds=timeout_seconds,
        )

    return endpoints


def load_config() -> Config:
    """Load configuration from paired store or legacy .env path."""

    # Try paired config path first
    store = LocalConfigStore()
    if store.is_paired():
        try:
            return _load_paired_config(store)
        except Exception as e:
            logging.warning(f"Failed to load paired config, falling back to legacy: {e}")
            # Fall through to legacy path

    # Fall back to legacy env path
    return _load_legacy_env_config()


def _load_paired_config(store: LocalConfigStore) -> Config:
    """Load config from paired connector storage (app-data + keychain)."""

    secure = SecureStorage()
    config_json = store.load_config()

    if not config_json:
        raise ConfigError("No config found in paired storage")

    # Load configuration from JSON with fallbacks
    print_agent_url = config_json.get("print_agent_url") or config_json.get("printAgentUrl", "")
    print_agent_api_key = secure.get_shared_api_key()
    if not print_agent_api_key:
        # Try loading from .env as fallback
        load_dotenv()
        print_agent_api_key = os.getenv("PRINT_AGENT_API_KEY", "")

    if not print_agent_url or not print_agent_api_key:
        raise ConfigError("Missing required configuration: print_agent_url and print_agent_api_key")

    # Load ERP auth from keychain
    erp_auth_mode = config_json.get("erp_auth_mode") or config_json.get("erp", {}).get("auth", {}).get("mode", "none")

    if erp_auth_mode == "basic":
        username, password = secure.get_erp_basic_auth()
        erp_auth_basic_username = username
        erp_auth_basic_password = password
        erp_auth_bearer_token = None
    elif erp_auth_mode == "bearer":
        erp_auth_basic_username = None
        erp_auth_basic_password = None
        erp_auth_bearer_token = secure.get_erp_bearer_token()
    else:
        erp_auth_basic_username = None
        erp_auth_basic_password = None
        erp_auth_bearer_token = None

    # Parse ERP endpoints from config
    erp_endpoints_raw = config_json.get("erp_endpoints_json", "{}")
    if not erp_endpoints_raw and "erp" in config_json and "endpoints" in config_json["erp"]:
        # Convert from new format
        endpoints_dict = config_json["erp"]["endpoints"]
        erp_endpoints_raw = json.dumps(endpoints_dict)

    erp_default_timeout = config_json.get("erp_default_timeout_seconds", 10)
    erp_endpoints = parse_erp_endpoints(
        erp_endpoints_raw or "{}",
        default_timeout_seconds=float(erp_default_timeout),
    )

    config = Config(
        print_agent_url=print_agent_url,
        print_agent_api_key=print_agent_api_key,
        poll_interval_seconds=config_json.get("poll_interval_seconds", 2.0),
        printer_port=config_json.get("printer_port", 9100),
        printer_timeout_seconds=config_json.get("printer_timeout_seconds", 5.0),
        max_concurrent_jobs=config_json.get("max_concurrent_jobs", 3),
        workstation_id=store.get_workstation_id(),
        erp_enabled=config_json.get("erp_enabled", False),
        erp_endpoints=erp_endpoints,
        erp_auth_mode=erp_auth_mode,
        erp_auth_bearer_token=erp_auth_bearer_token,
        erp_auth_basic_username=erp_auth_basic_username,
        erp_auth_basic_password=erp_auth_basic_password,
        erp_auth_static_headers=config_json.get("erp_auth_static_headers", {}),
        erp_retry_attempts=config_json.get("erp_retry_attempts", 0),
        erp_retry_backoff_seconds=config_json.get("erp_retry_backoff_seconds", 1.0),
        erp_default_timeout_seconds=config_json.get("erp_default_timeout_seconds", 10.0),
        erp_timeout_max_seconds=config_json.get("erp_timeout_max_seconds", 30.0),
        erp_retry_max_attempts=config_json.get("erp_retry_max_attempts", 3),
        erp_retry_backoff_base_seconds=config_json.get("erp_retry_backoff_base_seconds", 1.0),
        erp_retry_backoff_max_seconds=config_json.get("erp_retry_backoff_max_seconds", 60.0),
        erp_retry_jitter_seconds=config_json.get("erp_retry_jitter_seconds", 1.0),
        erp_agent_enabled=config_json.get("erp_agent_enabled", False),
        erp_agent_url=config_json.get("erp_agent_url", ""),
        erp_agent_api_key=print_agent_api_key,  # Use same API key for ERP agent
        erp_agent_poll_interval_seconds=config_json.get("erp_agent_poll_interval_seconds", 2.0),
        erp_agent_max_concurrent_requests=config_json.get("erp_agent_max_concurrent_requests", 2),
    )

    state = store.load_state()
    logging.info(f"Loaded paired config (workstation: {state['workstation_id']}, warehouse: {state.get('warehouse_id', 'N/A')})")
    return config


def _load_legacy_env_config() -> Config:
    """Load config from .env/environment variables (legacy/dev mode)."""
    load_dotenv()
    max_jobs = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
    if max_jobs < 1:
        raise ValueError(f"MAX_CONCURRENT_JOBS must be at least 1, got {max_jobs}")

    # Load or generate workstation_id for tracking
    workstation_id = os.getenv("WORKSTATION_ID")
    if not workstation_id:
        import uuid
        workstation_id = str(uuid.uuid4())
        # Persist to .env file for future runs (only if not already present)
        try:
            # Check if WORKSTATION_ID already exists in .env to prevent duplicates
            env_file_exists = os.path.exists(".env")
            has_workstation_id = False
            if env_file_exists:
                with open(".env", "r", encoding="utf-8") as f:
                    if "WORKSTATION_ID=" in f.read():
                        has_workstation_id = True
                        logging.warning("WORKSTATION_ID found in .env but not loaded - check file permissions or format")

            if not has_workstation_id:
                with open(".env", "a", encoding="utf-8") as env_file:
                    env_file.write(f"\n# Auto-generated workstation identifier (do not edit manually)\n")
                    env_file.write(f"WORKSTATION_ID={workstation_id}\n")
                logging.info("Generated and saved new WORKSTATION_ID=%s", workstation_id)
        except Exception as exc:
            logging.warning("Could not save WORKSTATION_ID to .env file: %s", exc)

    erp_enabled = parse_bool_env("ERP_ENABLED", default=False)
    erp_default_timeout_seconds = float(os.getenv("ERP_DEFAULT_TIMEOUT_SECONDS", "10"))
    if erp_default_timeout_seconds <= 0:
        raise ValueError(
            f"ERP_DEFAULT_TIMEOUT_SECONDS must be > 0, got {erp_default_timeout_seconds}"
        )

    erp_endpoints_raw = os.getenv("ERP_ENDPOINTS_JSON", "{}").strip() or "{}"
    erp_endpoints = parse_erp_endpoints(
        erp_endpoints_raw,
        default_timeout_seconds=erp_default_timeout_seconds,
    )

    erp_auth_mode = os.getenv("ERP_AUTH_MODE", "none").strip().lower()
    if erp_auth_mode not in {"none", "bearer", "basic", "static_headers"}:
        raise ValueError(
            "ERP_AUTH_MODE must be one of: none, bearer, basic, static_headers"
        )

    erp_auth_bearer_token = os.getenv("ERP_AUTH_BEARER_TOKEN", "").strip() or None
    erp_auth_basic_username = os.getenv("ERP_AUTH_BASIC_USERNAME", "").strip() or None
    erp_auth_basic_password = os.getenv("ERP_AUTH_BASIC_PASSWORD", "").strip() or None

    erp_auth_static_headers_raw = os.getenv("ERP_AUTH_STATIC_HEADERS_JSON", "").strip()
    erp_auth_static_headers: Dict[str, str] = {}
    if erp_auth_static_headers_raw:
        try:
            static_headers = json.loads(erp_auth_static_headers_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ERP_AUTH_STATIC_HEADERS_JSON must be valid JSON: {exc}"
            ) from exc
        if not isinstance(static_headers, dict):
            raise ValueError("ERP_AUTH_STATIC_HEADERS_JSON must be a JSON object")
        erp_auth_static_headers = {
            str(k).strip(): str(v)
            for k, v in static_headers.items()
            if str(k).strip()
        }

    if erp_auth_mode == "bearer" and not erp_auth_bearer_token:
        raise ValueError("ERP_AUTH_MODE=bearer requires ERP_AUTH_BEARER_TOKEN")
    if erp_auth_mode == "basic" and (not erp_auth_basic_username or not erp_auth_basic_password):
        raise ValueError(
            "ERP_AUTH_MODE=basic requires ERP_AUTH_BASIC_USERNAME and ERP_AUTH_BASIC_PASSWORD"
        )
    if erp_auth_mode == "static_headers" and not erp_auth_static_headers:
        raise ValueError(
            "ERP_AUTH_MODE=static_headers requires ERP_AUTH_STATIC_HEADERS_JSON"
        )

    erp_retry_attempts = int(os.getenv("ERP_RETRY_ATTEMPTS", "0"))
    if erp_retry_attempts < 0:
        raise ValueError(f"ERP_RETRY_ATTEMPTS must be >= 0, got {erp_retry_attempts}")

    erp_retry_backoff_seconds = float(os.getenv("ERP_RETRY_BACKOFF_SECONDS", "1"))
    if erp_retry_backoff_seconds < 0:
        raise ValueError(
            f"ERP_RETRY_BACKOFF_SECONDS must be >= 0, got {erp_retry_backoff_seconds}"
        )

    # Advanced ERP retry settings (optional)
    erp_timeout_max_seconds = float(os.getenv("ERP_TIMEOUT_MAX_SECONDS", "30"))
    if erp_timeout_max_seconds <= 0:
        raise ValueError(
            f"ERP_TIMEOUT_MAX_SECONDS must be > 0, got {erp_timeout_max_seconds}"
        )

    erp_retry_max_attempts = int(os.getenv("ERP_RETRY_MAX_ATTEMPTS", "3"))
    if erp_retry_max_attempts < 1:
        raise ValueError(
            f"ERP_RETRY_MAX_ATTEMPTS must be >= 1, got {erp_retry_max_attempts}"
        )

    erp_retry_backoff_base_seconds = float(os.getenv("ERP_RETRY_BACKOFF_BASE_SECONDS", "1"))
    if erp_retry_backoff_base_seconds < 0:
        raise ValueError(
            f"ERP_RETRY_BACKOFF_BASE_SECONDS must be >= 0, got {erp_retry_backoff_base_seconds}"
        )

    erp_retry_backoff_max_seconds = float(os.getenv("ERP_RETRY_BACKOFF_MAX_SECONDS", "60"))
    if erp_retry_backoff_max_seconds < 0:
        raise ValueError(
            f"ERP_RETRY_BACKOFF_MAX_SECONDS must be >= 0, got {erp_retry_backoff_max_seconds}"
        )

    erp_retry_jitter_seconds = float(os.getenv("ERP_RETRY_JITTER_SECONDS", "1"))
    if erp_retry_jitter_seconds < 0:
        raise ValueError(
            f"ERP_RETRY_JITTER_SECONDS must be >= 0, got {erp_retry_jitter_seconds}"
        )

    # ERP-agent specific settings
    erp_agent_enabled = parse_bool_env("ERP_AGENT_ENABLED", default=False)
    erp_agent_url = os.getenv("ERP_AGENT_URL", "").strip()
    erp_agent_api_key = os.getenv("ERP_AGENT_API_KEY", "").strip() or ""
    erp_agent_poll_interval = float(os.getenv("ERP_AGENT_POLL_INTERVAL_SECONDS", "2"))
    if erp_agent_poll_interval < 0:
        raise ValueError(
            f"ERP_AGENT_POLL_INTERVAL_SECONDS must be >= 0, got {erp_agent_poll_interval}"
        )
    erp_agent_max_concurrent = int(os.getenv("ERP_AGENT_MAX_CONCURRENT_REQUESTS", "2"))
    if erp_agent_max_concurrent < 1:
        raise ValueError(
            f"ERP_AGENT_MAX_CONCURRENT_REQUESTS must be >= 1, got {erp_agent_max_concurrent}"
        )

    config = Config(
        print_agent_url=require_env("PRINT_AGENT_CALLBACK_URL"),
        print_agent_api_key=require_env("PRINT_AGENT_API_KEY"),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "2")),
        printer_port=int(os.getenv("PRINTER_PORT", "9100")),
        printer_timeout_seconds=float(os.getenv("PRINTER_TIMEOUT_SECONDS", "5")),
        max_concurrent_jobs=max_jobs,
        workstation_id=workstation_id,
        erp_enabled=erp_enabled,
        erp_endpoints=erp_endpoints,
        erp_auth_mode=erp_auth_mode,
        erp_auth_bearer_token=erp_auth_bearer_token,
        erp_auth_basic_username=erp_auth_basic_username,
        erp_auth_basic_password=erp_auth_basic_password,
        erp_auth_static_headers=erp_auth_static_headers,
        erp_retry_attempts=erp_retry_attempts,
        erp_retry_backoff_seconds=erp_retry_backoff_seconds,
        erp_default_timeout_seconds=erp_default_timeout_seconds,
        erp_timeout_max_seconds=erp_timeout_max_seconds,
        erp_retry_max_attempts=erp_retry_max_attempts,
        erp_retry_backoff_base_seconds=erp_retry_backoff_base_seconds,
        erp_retry_backoff_max_seconds=erp_retry_backoff_max_seconds,
        erp_retry_jitter_seconds=erp_retry_jitter_seconds,
        erp_agent_enabled=erp_agent_enabled,
        erp_agent_url=erp_agent_url,
        erp_agent_api_key=erp_agent_api_key,
        erp_agent_poll_interval_seconds=erp_agent_poll_interval,
        erp_agent_max_concurrent_requests=erp_agent_max_concurrent,
    )
    logging.info("Loaded config from .env (unpaired or development mode)")
    return config


def write_pid_file() -> None:
    """Write current process ID to PID file in app-data directory."""
    store = LocalConfigStore()
    pid = os.getpid()
    try:
        with open(store.get_pid_path(), "w", encoding="utf-8") as f:
            f.write(str(pid))
        logging.info("PID file created: %s (PID=%d)", store.get_pid_path(), pid)
    except Exception as exc:
        logging.warning("Could not create PID file: %s", exc)


def remove_pid_file() -> None:
    """Remove PID file on clean shutdown."""
    store = LocalConfigStore()
    try:
        if store.get_pid_path().exists():
            store.get_pid_path().unlink()
            logging.info("PID file removed: %s", store.get_pid_path())
    except Exception as exc:
        logging.warning("Could not remove PID file: %s", exc)


def check_pid_file() -> bool:
    """Check if another instance is already running.

    Returns True if another instance is running, False otherwise.
    Also cleans up stale PID files (>1 hour old) to prevent issues after crashes.
    """
    store = LocalConfigStore()
    pid_file = store.get_pid_path()

    if not pid_file.exists():
        return False

    try:
        # Check if PID file is stale (crashed process left PID file behind)
        import time
        pid_file_age = time.time() - pid_file.stat().st_mtime
        if pid_file_age > 3600:  # 1 hour
            logging.warning("Found stale PID file (age=%d seconds) - removing", pid_file_age)
            try:
                pid_file.unlink()
                logging.info("Stale PID file removed")
            except Exception as exc:
                logging.warning("Could not remove stale PID file: %s", exc)
            return False

        with open(pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())

        # Check if process is still running
        if sys.platform == "win32":
            import psutil
            try:
                psutil.Process(pid)
                return True  # Process exists
            except psutil.NoSuchProcess:
                return False  # Process doesn't exist
        else:
            # Unix-like systems
            try:
                os.kill(pid, 0)  # Check if process exists
                return True
            except OSError:
                return False
    except Exception as exc:
        logging.warning("Error checking PID file: %s", exc)
        return False


def setup_logging(daemon: bool = False) -> None:
    """Setup logging configuration.

    Args:
        daemon: If True, log to file only. If False, log to console.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = "%(asctime)s %(levelname)s %(message)s"

    store = LocalConfigStore()
    if daemon or getattr(sys, "frozen", False):
        # Background mode: log to file
        log_file = Path(os.getenv("LOG_FILE", str(store.get_log_path())))
        if not log_file.is_absolute():
            log_file = store.app_data_dir / log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=log_file,
            filemode="a",  # Append mode
            force=True,
        )
        logging.info("File logging started - logging to file: %s", log_file)
    else:
        # Foreground mode: log to console
        logging.basicConfig(
            level=log_level,
            format=log_format,
            force=True,
        )


def setup_startup_logging(force_file: bool = False) -> Path:
    """Initialize logging early so startup branch decisions are captured."""
    store = LocalConfigStore()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = "%(asctime)s %(levelname)s %(message)s"
    handlers: List[logging.Handler] = []

    if force_file:
        log_file = Path(os.getenv("LOG_FILE", str(store.get_log_path())))
        if not log_file.is_absolute():
            log_file = store.app_data_dir / log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers, force=True)
    logging.info(
        "Startup logging initialized mode=%s frozen=%s app_data_dir=%s cwd=%s exe_dir=%s",
        "file" if force_file else "console",
        getattr(sys, "frozen", False),
        store.app_data_dir,
        Path.cwd(),
        get_exe_dir(),
    )
    return store.get_log_path()


def _clear_connector_secrets() -> None:
    secure = SecureStorage()
    secure.clear_all()
    logging.info("Cleared connector secrets from secure storage")


def _sanitize_env_value_for_log(key: str, value: str) -> str:
    sensitive_markers = ("key", "token", "password", "secret", "authorization")
    if any(marker in key.lower() for marker in sensitive_markers):
        return "[redacted]"
    if len(value) > 200:
        return f"{value[:200]}... [truncated]"
    return value


def _resolve_bootstrap_pairing_config() -> Config:
    """Load and validate the bootstrap config required to relaunch setup."""
    ensure_env_file_exists()
    try:
        config = _load_legacy_env_config()
    except Exception as exc:
        raise ConfigError(f"Bootstrap config is unavailable: {exc}") from exc

    if not config.print_agent_url or not config.print_agent_api_key:
        raise ConfigError("Bootstrap config must include PRINT_AGENT_CALLBACK_URL and PRINT_AGENT_API_KEY")

    return config


def _clear_local_pairing_state(store: LocalConfigStore) -> None:
    """Clear paired local state and cached secrets/runtime files."""
    store.reset_paired_state()
    store.clear_local_runtime_state()
    _clear_connector_secrets()
    logging.info("Cleared local pairing state and runtime files")


def _launch_setup_wizard(manager: ConnectorManager, args: Any, reason: str, success_message: Optional[str] = None) -> None:
    logging.info("Setup wizard path selected reason=%s console=%s tkinter=%s", reason, args.console, TKINTER_AVAILABLE)

    if args.console:
        print("\n=== CONSOLE MODE PAIRING ===\n")
        if console_pairing(manager):
            logging.info("Console pairing completed successfully")
            if success_message:
                print(success_message)
            pause_for_debug()
            sys.exit(0)
        logging.error("Console pairing failed")
        pause_for_debug()
        sys.exit(1)

    if not TKINTER_AVAILABLE:
        logging.error("Setup requested but Tkinter is unavailable")
        show_error_message(
            "Setup Wizard Error",
            "Tkinter GUI is not available.\n\nThe setup wizard requires Tkinter.\n\nPlease use --console or contact support.",
        )
        sys.exit(1)

    try:
        wizard = SetupWizard(manager)
        paired_successfully = wizard.run()
        if paired_successfully:
            logging.info("GUI setup wizard completed successfully")
            if success_message:
                show_info_message("Setup Complete", success_message)
            sys.exit(0)

        logging.warning("GUI setup wizard exited without pairing close_reason=%s", wizard.close_reason)
        sys.exit(1)
    except Exception as exc:
        logging.exception("GUI setup wizard failed, falling back to console: %s", exc)
        print(f"\nGUI setup wizard failed: {exc}")
        print("\nFalling back to console-based pairing...\n")
        if console_pairing(manager):
            logging.info("Console pairing fallback completed successfully")
            sys.exit(0)
        logging.error("Console pairing fallback failed")
        sys.exit(1)


def _reset_pairing_state(args: Any) -> None:
    store = LocalConfigStore()
    setup_startup_logging(force_file=getattr(sys, "frozen", False))
    logging.info("Reset pairing requested")

    if check_pid_file():
        logging.error("Reset pairing refused because another instance is already running")
        print("ERROR: Another instance is already running.")
        print("Use --stop to stop the existing instance first.")
        sys.exit(1)

    try:
        bootstrap_config = _resolve_bootstrap_pairing_config()
    except Exception as exc:
        logging.error("Reset pairing refused because bootstrap validation failed: %s", exc)
        print(f"ERROR: Cannot reset pairing because bootstrap configuration is invalid: {exc}")
        sys.exit(1)

    _clear_local_pairing_state(store)
    manager = ConnectorManager(bootstrap_config.print_agent_url, bootstrap_config.print_agent_api_key)
    _launch_setup_wizard(
        manager,
        args,
        reason="explicit-reset-pairing",
        success_message="Connector paired successfully!\n\nPlease run qc-print-agent.exe again to start processing jobs.\n\nFor background mode, use: qc-print-agent.exe --daemon",
    )


     


def main() -> None:
    """Main entry point with support for daemon/background mode."""
    import argparse

    parser = argparse.ArgumentParser(description="QC Print Agent - ZPL printer agent")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as background daemon (logs to file, detach from terminal)"
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop running daemon instance"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if daemon is running"
    )

    # NEW: Setup and testing arguments for warehouse connector
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Launch first-run setup wizard for pairing"
    )
    parser.add_argument(
        "--reset-pairing",
        action="store_true",
        help="Clear local pairing state and relaunch setup"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Use console-based pairing instead of GUI"
    )
    parser.add_argument(
        "--test-print",
        metavar="PRINTER_IP",
        help="Test printer connection (e.g., --test-print 10.0.0.25)"
    )
    parser.add_argument(
        "--test-erp",
        metavar="ENDPOINT_KEY",
        help="Test ERP connection (e.g., --test-erp erp_box_fetch)"
    )

    args = parser.parse_args()

    # Load .env before logging so log-file configuration is available early.
    load_dotenv()
    startup_log_path = setup_startup_logging(force_file=getattr(sys, "frozen", False) and not args.console)
    logging.info(
        "Process startup frozen=%s args=%s startup_log=%s",
        getattr(sys, "frozen", False),
        vars(args),
        startup_log_path,
    )

    # Handle --stop command
    if args.stop:
        if not os.path.exists(LocalConfigStore().get_pid_path()):
            print("No PID file found - agent may not be running")
            sys.exit(1)

        try:
            with open(LocalConfigStore().get_pid_path(), "r", encoding="utf-8") as f:
                pid = int(f.read().strip())

            print(f"Stopping print agent (PID={pid})...")
            if sys.platform == "win32":
                import psutil
                psutil.Process(pid).terminate()
            else:
                os.kill(pid, SIGTERM)

            # Wait for process to terminate
            for _ in range(10):
                time.sleep(0.5)
                try:
                    if sys.platform == "win32":
                        psutil.Process(pid)
                    else:
                        os.kill(pid, 0)
                except:
                    break  # Process terminated
            else:
                print("WARNING: Process did not terminate gracefully")

            print("Print agent stopped")
        except Exception as exc:
            print(f"Error stopping agent: {exc}")
            sys.exit(1)
        return

    # Handle --status command
    if args.status:
        if check_pid_file():
            try:
                with open(LocalConfigStore().get_pid_path(), "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                print(f"Print agent is running (PID={pid})")
            except Exception:
                print("Print agent status unknown (PID file exists but unreadable)")
        else:
            print("Print agent is not running")
        return

    if args.reset_pairing:
        _reset_pairing_state(args)
        return

    # NEW: Handle --setup command
    if args.setup:
        try:
            config = load_config()
            manager = ConnectorManager(config.print_agent_url, config.print_agent_api_key)
            _launch_setup_wizard(
                manager,
                args,
                reason="explicit-setup",
                success_message="Connector paired successfully!\n\nPlease run qc-print-agent.exe again to start processing jobs.\n\nFor background mode, use: qc-print-agent.exe --daemon",
            )
        except Exception as exc:
            logging.exception("Explicit setup failed before wizard launch: %s", exc)
            print(f"\nSetup error: {exc}")
            pause_for_debug()
            sys.exit(1)
        return

    # NEW: Handle --test-print command
    if args.test_print:
        try:
            config = load_config()
            agent = PrintAgent(config)
            result = agent.test_printer_connection(args.test_print)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result.get("success") else 1)
        except Exception as exc:
            print(f"Printer test error: {exc}")
            sys.exit(1)
        return

    # NEW: Handle --test-erp command
    if args.test_erp:
        try:
            config = load_config()
            agent = PrintAgent(config)
            result = agent.test_erp_connection(args.test_erp)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result.get("success") else 1)
        except Exception as exc:
            print(f"ERP test error: {exc}")
            sys.exit(1)
        return

    # Check if connector is paired - if not, launch setup wizard automatically
    store = LocalConfigStore()
    logging.info(
        "Resolved startup state app_data_dir=%s state_path=%s config_path=%s log_path=%s paired=%s",
        store.app_data_dir,
        store.state_file,
        store.config_file,
        store.get_log_path(),
        store.is_paired(),
    )
    if not store.is_paired():
        logging.info("First-run setup branch selected")
        print("QC Print Agent - First Run Setup")
        print("=" * 40)
        print("This appears to be your first time running the agent.")
        print("Launching setup wizard to pair with your warehouse...")
        print()

        # Ensure .env file exists (create from .env.example or minimal defaults)
        ensure_env_file_exists()

        # Check if tkinter is available
        if not TKINTER_AVAILABLE:
            show_error_message(
                "Setup Wizard Error",
                "Tkinter GUI is not available.\n\nThe setup wizard requires Tkinter.\n\nPlease install python3-tk or contact support."
            )
            sys.exit(1)

        try:
            config = load_config()
            manager = ConnectorManager(config.print_agent_url, config.print_agent_api_key)
            _launch_setup_wizard(
                manager,
                args,
                reason="auto-first-run",
                success_message="Connector paired successfully!\n\nPlease run qc-print-agent.exe again to start processing jobs.\n\nFor background mode, use: qc-print-agent.exe --daemon",
            )
        except Exception as exc:
            logging.exception("Auto first-run setup failed before wizard launch: %s", exc)
            print(f"\nSetup failed: {exc}")
            sys.exit(1)

    # Device is paired locally - verify it's still registered on server
    # (handles case where device was unpaired from frontend)
    logging.info("Already-paired startup branch selected")
    print("QC Print Agent - Verifying registration...")
    try:
        config = load_config()
        manager = ConnectorManager(config.print_agent_url, config.print_agent_api_key)

        if not manager.verify_registration():
            print("Device is no longer registered on the server.")
            print("Resetting local state and launching setup wizard...")
            print()

            _clear_local_pairing_state(store)

            # Launch setup wizard
            ensure_env_file_exists()
            _launch_setup_wizard(
                manager,
                args,
                reason="registration-reset",
                success_message="Connector paired successfully!\n\nPlease run qc-print-agent.exe again to start processing jobs.",
            )
    except Exception as exc:
        # On network errors, continue anyway (might be temporary)
        logging.warning("Registration verification failed, continuing with local state: %s", exc)
        print(f"Warning: Could not verify registration: {exc}")
        print("Continuing with local state...")
        print()

    # Check for existing instance
    if check_pid_file():
        print("ERROR: Another instance is already running!")
        print("Use --stop to stop the existing instance first")
        sys.exit(1)

    # Setup logging based on mode
    setup_logging(daemon=args.daemon)
    logging.info(
        "Runtime logging configured mode=%s frozen=%s paired=%s",
        "daemon" if args.daemon else ("packaged-gui" if getattr(sys, "frozen", False) else "console"),
        getattr(sys, "frozen", False),
        store.is_paired(),
    )

    # Register PID file cleanup on exit
    atexit.register(remove_pid_file)

    # Write PID file
    write_pid_file()

    if args.daemon:
        print(f"Starting print agent in background mode (PID={os.getpid()})")
        print("Logs are being written to: print_agent.log")
        print("Use --stop to stop the daemon")
        print("Use --status to check if it's running")

    try:
        # Load config and start agent
        config = load_config()
        agent = PrintAgent(config)

        # Start ERP-agent loop in background thread if enabled
        erp_thread = None
        if config.erp_agent_enabled:
            erp_thread = threading.Thread(target=agent.run_erp_forever, daemon=True, name="ErpAgentLoop")
            erp_thread.start()
            logging.info("Started ERP-agent loop in background thread")

        # Start heartbeat loop if paired (for warehouse connector)
        store = LocalConfigStore()
        if store.is_paired():
            try:
                connector = ConnectorManager(
                    api_base=config.print_agent_url,
                    shared_api_key=config.print_agent_api_key
                )
                heartbeat_thread = threading.Thread(
                    target=connector.run_heartbeat_loop,
                    daemon=True,
                    name="HeartbeatLoop"
                )
                heartbeat_thread.start()
                logging.info(f"[CONNECTOR] Started heartbeat loop (workstation: {connector.workstation_id})")
            except Exception as exc:
                logging.warning(f"[CONNECTOR] Failed to start heartbeat loop: {exc}")

        # Run print loop in main thread
        agent.run_forever()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as exc:
        logging.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
