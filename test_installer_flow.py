#!/usr/bin/env python3
"""Tests for setup/reset and installer-oriented startup helpers."""

from __future__ import annotations

import os
import shutil
import time
import unittest
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import print_agent
from print_agent import Config, ConfigError, ErpEndpointConfig, LocalConfigStore, PrintAgent, SecureStorage, SetupWizard


class SetupWizardBehaviorTests(unittest.TestCase):
    def test_on_cancel_marks_close_reason_without_success(self) -> None:
        wizard = SetupWizard.__new__(SetupWizard)
        wizard.paired_successfully = False
        wizard.close_reason = "unknown"
        wizard.root = Mock()

        wizard.on_cancel()

        self.assertEqual(wizard.close_reason, "cancel")
        wizard.root.destroy.assert_called_once()

    def test_on_window_close_marks_close_reason_without_success(self) -> None:
        wizard = SetupWizard.__new__(SetupWizard)
        wizard.paired_successfully = False
        wizard.close_reason = "unknown"
        wizard.root = Mock()

        wizard.on_window_close()

        self.assertEqual(wizard.close_reason, "window_close")
        wizard.root.destroy.assert_called_once()


class InstallerHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        signal_patcher = patch.object(PrintAgent, "_setup_signal_handlers", autospec=True)
        self.addCleanup(signal_patcher.stop)
        signal_patcher.start()

    def test_save_config_to_env_writes_required_values_from_dict_config(self) -> None:
        store = LocalConfigStore()
        config = {
            "edgeFunctions": {
                "sharedUrl": "https://example.com/functions/v1/print-agent",
                "apiKey": "shared-key",
            },
            "poll_interval_seconds": 5,
            "max_concurrent_jobs": 2,
            "printer_port": 9109,
            "printer_timeout_seconds": 7,
        }

        tmpdir = Path("test-output-save-config")
        if tmpdir.exists():
            shutil.rmtree(tmpdir)
        tmpdir.mkdir(parents=True)
        original_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            store.save_config_to_env(config)
            env_text = Path(".env").read_text(encoding="utf-8")
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmpdir)

        self.assertIn("PRINT_AGENT_CALLBACK_URL=https://example.com/functions/v1/print-agent", env_text)
        self.assertIn("PRINT_AGENT_API_KEY=shared-key", env_text)
        self.assertIn("POLL_INTERVAL_SECONDS=5", env_text)
        self.assertIn("PRINTER_PORT=9109", env_text)

    def test_sanitize_env_value_for_log_redacts_sensitive_values(self) -> None:
        self.assertEqual(print_agent._sanitize_env_value_for_log("PRINT_AGENT_API_KEY", "secret-value"), "[redacted]")
        self.assertEqual(print_agent._sanitize_env_value_for_log("ERP_AUTH_BASIC_PASSWORD", "secret-pass"), "[redacted]")
        self.assertEqual(print_agent._sanitize_env_value_for_log("LOG_LEVEL", "INFO"), "INFO")

    def test_load_env_file_logs_redacted_sensitive_values(self) -> None:
        env_path = Path("test-output-load-env.env")
        env_path.write_text(
            "PRINT_AGENT_API_KEY=top-secret\nLOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        try:
            with patch.object(print_agent, "logging") as logging_mock:
                print_agent._load_env_file(env_path)
        finally:
            env_path.unlink(missing_ok=True)

        logged_messages = [call.args for call in logging_mock.info.call_args_list]
        self.assertIn(("Loading env var from .env: %s=%s", "PRINT_AGENT_API_KEY", "[redacted]"), logged_messages)
        self.assertIn(("Loading env var from .env: %s=%s", "LOG_LEVEL", "INFO"), logged_messages)

    def test_ensure_env_file_exists_uses_internal_env_example_for_frozen_build(self) -> None:
        exe_dir = Path("test-output-frozen-exe")
        internal_dir = exe_dir / "_internal"
        if exe_dir.exists():
            shutil.rmtree(exe_dir)
        internal_dir.mkdir(parents=True)
        (internal_dir / ".env.example").write_text(
            "PRINT_AGENT_CALLBACK_URL=https://example.com/functions/v1/print-agent\n"
            "PRINT_AGENT_API_KEY=shared-key\n",
            encoding="utf-8",
        )

        try:
            with patch.object(print_agent, "get_exe_dir", return_value=exe_dir), \
                 patch.object(print_agent.Path, "cwd", return_value=exe_dir), \
                 patch.object(print_agent.sys, "frozen", True, create=True):
                print_agent.ensure_env_file_exists()

            env_text = (exe_dir / ".env").read_text(encoding="utf-8")
        finally:
            if exe_dir.exists():
                shutil.rmtree(exe_dir)

        self.assertIn("PRINT_AGENT_CALLBACK_URL=https://example.com/functions/v1/print-agent", env_text)
        self.assertIn("PRINT_AGENT_API_KEY=shared-key", env_text)

    def test_ensure_env_file_exists_heals_partial_packaged_env(self) -> None:
        exe_dir = Path("test-output-heal-env")
        if exe_dir.exists():
            shutil.rmtree(exe_dir)
        exe_dir.mkdir(parents=True)
        (exe_dir / ".env").write_text(
            "# Auto-generated workstation identifier (do not edit manually)\n"
            "WORKSTATION_ID=test-only\n",
            encoding="utf-8",
        )
        (exe_dir / ".env.example").write_text(
            "PRINT_AGENT_CALLBACK_URL=https://example.com/functions/v1/print-agent\n"
            "PRINT_AGENT_API_KEY=shared-key\n",
            encoding="utf-8",
        )

        try:
            with patch.object(print_agent, "get_exe_dir", return_value=exe_dir), \
                 patch.object(print_agent.Path, "cwd", return_value=exe_dir), \
                 patch.object(print_agent.sys, "frozen", True, create=True):
                print_agent.ensure_env_file_exists()

            env_text = (exe_dir / ".env").read_text(encoding="utf-8")
        finally:
            if exe_dir.exists():
                shutil.rmtree(exe_dir)

        self.assertIn("PRINT_AGENT_CALLBACK_URL=https://example.com/functions/v1/print-agent", env_text)
        self.assertIn("PRINT_AGENT_API_KEY=shared-key", env_text)
        self.assertNotIn("WORKSTATION_ID=test-only", env_text)

    def test_launch_setup_wizard_success_uses_info_message(self) -> None:
        args = SimpleNamespace(console=False)
        manager = Mock()
        wizard = Mock()
        wizard.run.return_value = True

        with patch.object(print_agent, "SetupWizard", return_value=wizard), \
             patch.object(print_agent, "TKINTER_AVAILABLE", True), \
             patch.object(print_agent, "show_info_message") as show_info, \
             patch.object(print_agent, "show_error_message") as show_error, \
             patch.object(print_agent, "sys") as mock_sys:
            mock_sys.exit.side_effect = SystemExit(0)

            with self.assertRaises(SystemExit) as exc:
                print_agent._launch_setup_wizard(manager, args, "test-success", "paired!")

        self.assertEqual(exc.exception.code, 0)
        show_info.assert_called_once_with("Setup Complete", "paired!")
        show_error.assert_not_called()

    def test_launch_setup_wizard_cancel_exits_non_success_without_popup(self) -> None:
        args = SimpleNamespace(console=False)
        manager = Mock()
        wizard = Mock()
        wizard.run.return_value = False
        wizard.close_reason = "cancel"

        with patch.object(print_agent, "SetupWizard", return_value=wizard), \
             patch.object(print_agent, "TKINTER_AVAILABLE", True), \
             patch.object(print_agent, "show_info_message") as show_info, \
             patch.object(print_agent, "show_error_message") as show_error, \
             patch.object(print_agent, "sys") as mock_sys:
            mock_sys.exit.side_effect = SystemExit(1)

            with self.assertRaises(SystemExit) as exc:
                print_agent._launch_setup_wizard(manager, args, "test-cancel", "paired!")

        self.assertEqual(exc.exception.code, 1)
        show_info.assert_not_called()
        show_error.assert_not_called()

    def test_pause_for_debug_skips_input_when_no_tty(self) -> None:
        stdin_mock = Mock()
        stdin_mock.isatty.return_value = False

        with patch.object(print_agent.sys, "frozen", True, create=True), \
             patch.object(print_agent.sys, "stdin", stdin_mock), \
             patch.object(builtins, "input") as input_mock:
            print_agent.pause_for_debug()

        input_mock.assert_not_called()

    def test_reset_pairing_refuses_when_bootstrap_validation_fails(self) -> None:
        args = SimpleNamespace(console=False)
        store = Mock()

        with patch.object(print_agent, "LocalConfigStore", return_value=store), \
             patch.object(print_agent, "setup_startup_logging"), \
             patch.object(print_agent, "check_pid_file", return_value=False), \
             patch.object(print_agent, "_resolve_bootstrap_pairing_config", side_effect=ConfigError("bad bootstrap")), \
             patch.object(print_agent, "_clear_connector_secrets") as clear_secrets, \
             patch.object(print_agent, "_launch_setup_wizard") as launch_setup, \
             patch.object(print_agent, "sys") as mock_sys:
            mock_sys.exit.side_effect = SystemExit(1)

            with self.assertRaises(SystemExit) as exc:
                print_agent._reset_pairing_state(args)

        self.assertEqual(exc.exception.code, 1)
        store.reset_paired_state.assert_not_called()
        clear_secrets.assert_not_called()
        launch_setup.assert_not_called()

    def test_reset_pairing_validates_then_clears_and_launches_setup(self) -> None:
        args = SimpleNamespace(console=False)
        store = Mock()
        bootstrap_config = Config(
            print_agent_url="https://example.com/functions/v1/print-agent",
            print_agent_api_key="shared-key",
        )

        with patch.object(print_agent, "LocalConfigStore", return_value=store), \
             patch.object(print_agent, "setup_startup_logging"), \
             patch.object(print_agent, "check_pid_file", return_value=False), \
             patch.object(print_agent, "_resolve_bootstrap_pairing_config", return_value=bootstrap_config), \
             patch.object(print_agent, "_clear_connector_secrets") as clear_secrets, \
             patch.object(print_agent, "ConnectorManager") as manager_cls, \
             patch.object(print_agent, "_launch_setup_wizard") as launch_setup:
            print_agent._reset_pairing_state(args)

        store.reset_paired_state.assert_called_once()
        store.clear_local_runtime_state.assert_called_once()
        clear_secrets.assert_called_once()
        manager_cls.assert_called_once_with(bootstrap_config.print_agent_url, bootstrap_config.print_agent_api_key)
        launch_setup.assert_called_once()

    def test_clear_local_pairing_state_cleans_store_and_secrets(self) -> None:
        store = Mock()
        with patch.object(print_agent, "_clear_connector_secrets") as clear_secrets:
            print_agent._clear_local_pairing_state(store)

        store.reset_paired_state.assert_called_once()
        store.clear_local_runtime_state.assert_called_once()
        clear_secrets.assert_called_once()

    def test_handle_remote_disconnect_marks_server_unpaired_and_sets_event(self) -> None:
        app_dir = Path("test-output-remote-unpaired")
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True)
        event_was_set = False

        try:
            with patch.object(LocalConfigStore, "get_app_data_dir", return_value=app_dir), \
                 patch.object(print_agent, "_clear_connector_secrets") as clear_secrets, \
                 patch.object(print_agent, "show_info_message") as show_info, \
                 patch.object(print_agent.ConnectorManager, "acknowledge_unpair", return_value=True) as ack_unpair, \
                 patch.object(SecureStorage, "get_control_plane_token", return_value="token"):
                print_agent.REMOTE_UNPAIRED_EVENT.clear()
                manager = print_agent.ConnectorManager("https://example.com/functions/v1/print-agent", "shared-key")
                manager.store.save_state({
                    "workstation_id": "ws-1",
                    "is_paired": True,
                    "connection_status": "paired_active",
                    "paired_at": "2026-01-01T00:00:00+00:00",
                    "warehouse_id": "wh-1",
                    "station_name": "Station A",
                })

                manager.handle_remote_disconnect("device_unpaired_by_admin")

                state = manager.store.load_state()
                event_was_set = print_agent.REMOTE_UNPAIRED_EVENT.is_set()
        finally:
            print_agent.REMOTE_UNPAIRED_EVENT.clear()
            if app_dir.exists():
                shutil.rmtree(app_dir)

        self.assertFalse(state["is_paired"])
        self.assertEqual(state["connection_status"], "server_unpaired")
        self.assertEqual(state["disconnect_reason"], "device_unpaired_by_admin")
        self.assertTrue(event_was_set)
        ack_unpair.assert_called_once()
        clear_secrets.assert_called_once()
        show_info.assert_called_once()

    def test_acknowledge_unpair_posts_expected_request(self) -> None:
        response = Mock()
        response.status_code = 200
        app_dir = Path("test-output-ack-unpair")
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True)

        try:
            with patch.object(LocalConfigStore, "get_app_data_dir", return_value=app_dir), \
                 patch.object(SecureStorage, "get_control_plane_token", return_value="control-token"), \
                 patch.object(print_agent.requests, "post", return_value=response) as post_request:
                manager = print_agent.ConnectorManager("https://example.com/functions/v1/print-agent", "shared-key")
                manager.workstation_id = "ws-ack-1"

                result = manager.acknowledge_unpair()
        finally:
            if app_dir.exists():
                shutil.rmtree(app_dir)

        self.assertTrue(result)
        _, kwargs = post_request.call_args
        self.assertEqual(kwargs["json"], {"workstationId": "ws-ack-1"})
        self.assertEqual(kwargs["headers"]["X-API-Key"], "shared-key")
        self.assertEqual(kwargs["headers"]["X-Control-Plane-Token"], "control-token")

    def test_check_pid_file_removes_stale_pid_file(self) -> None:
        app_dir = Path("test-output-stale-pid")
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True)
        pid_file = app_dir / "print_agent.pid"
        pid_file.write_text("12345", encoding="utf-8")
        stale_time = time.time() - 7200
        os.utime(pid_file, (stale_time, stale_time))

        try:
            with patch.object(LocalConfigStore, "get_app_data_dir", return_value=app_dir):
                result = print_agent.check_pid_file()
        finally:
            if app_dir.exists():
                shutil.rmtree(app_dir)

        self.assertFalse(result)
        self.assertFalse(pid_file.exists())

    def test_test_erp_connection_uses_current_helper_signature(self) -> None:
        config = Config(
            print_agent_url="https://example.com/functions/v1/print-agent",
            print_agent_api_key="test-api-key",
            workstation_id="ws-test-01",
            erp_enabled=True,
            erp_endpoints={
                "completion_event": ErpEndpointConfig(
                    url="https://erp.example.com/completion",
                    method="POST",
                    timeout_seconds=10,
                )
            },
        )
        agent = PrintAgent(config)

        with patch.object(
            agent,
            "_send_erp_http_request",
            return_value={"status_code": 200, "body": "{}", "correlation_id": "corr", "idempotency_key": "idem"},
        ) as send_request:
            result = agent.test_erp_connection("completion_event", {"test": True})

        self.assertTrue(result["success"])
        _, kwargs = send_request.call_args
        self.assertEqual(kwargs["endpoint_key"], "completion_event")
        self.assertEqual(kwargs["payload"], {"test": True})
        self.assertIn("correlation_id", kwargs)
        self.assertIn("idempotency_key", kwargs)


if __name__ == "__main__":
    unittest.main()
