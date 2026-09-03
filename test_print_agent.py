#!/usr/bin/env python3
"""Focused regression tests for PrintAgent job completion callbacks."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from print_agent import Config, ErpEndpointConfig, PrintAgent


def build_config() -> Config:
    return Config(
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


class PrintAgentProcessJobTests(unittest.TestCase):
    def setUp(self) -> None:
        signal_patcher = patch.object(PrintAgent, "_setup_signal_handlers", autospec=True)
        self.addCleanup(signal_patcher.stop)
        signal_patcher.start()
        self.agent = PrintAgent(build_config())

    def test_process_job_preserves_erp_success_context_in_callback(self) -> None:
        job = {
            "id": "job-erp-1",
            "job_type": "erp",
            "erp_endpoint_key": "completion_event",
            "erp_payload": {"message_id": "msg-1"},
        }

        with patch.object(
            self.agent,
            "_send_to_erp",
            return_value={
                "status_code": 202,
                "correlation_id": "corr-123",
                "idempotency_key": "job-erp-1",
                "body": '{"status":"success"}',
            },
        ) as send_to_erp, patch.object(self.agent, "_notify_print_service") as notify:
            result = self.agent.process_job(job)

        self.assertTrue(result)
        send_to_erp.assert_called_once_with(job)
        notify.assert_called_once()
        _, kwargs = notify.call_args
        self.assertEqual(kwargs["status"], "completed")
        self.assertEqual(kwargs["correlation_id"], "corr-123")
        self.assertEqual(kwargs["erp_body"], '{"status":"success"}')
        self.assertEqual(kwargs["response_code"], 202)

    def test_process_job_printer_success_path_is_unchanged(self) -> None:
        job = {
            "id": "job-print-1",
            "printer_ip": "203.0.113.10",
            "printer_port": 9100,
            "zpl_data": "^XA^XZ",
        }

        with patch.object(self.agent, "_send_to_printer") as send_to_printer, patch.object(
            self.agent, "_notify_print_service"
        ) as notify:
            result = self.agent.process_job(job)

        self.assertTrue(result)
        send_to_printer.assert_called_once_with(job)
        notify.assert_called_once()
        _, kwargs = notify.call_args
        self.assertEqual(kwargs["status"], "completed")
        self.assertEqual(kwargs["response_code"], 200)
        self.assertIsNone(kwargs["correlation_id"])
        self.assertIsNone(kwargs["erp_body"])

    def test_process_job_marks_failed_when_erp_send_raises(self) -> None:
        job = {
            "id": "job-erp-fail-1",
            "job_type": "erp",
            "erp_endpoint_key": "completion_event",
            "erp_payload": {"message_id": "msg-1"},
        }

        with patch.object(self.agent, "_send_to_erp", side_effect=RuntimeError("erp exploded")) as send_to_erp, patch.object(
            self.agent, "_mark_failed"
        ) as mark_failed, patch.object(self.agent, "_notify_print_service") as notify:
            result = self.agent.process_job(job)

        self.assertFalse(result)
        send_to_erp.assert_called_once_with(job)
        mark_failed.assert_called_once()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
