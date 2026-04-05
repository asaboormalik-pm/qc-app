#!/usr/bin/env python3
"""Regression tests for ERP-agent box fetch payload handling."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from print_agent import Config, ErpEndpointConfig, GenericErpRequest, PrintAgent


def build_config() -> Config:
    return Config(
        print_agent_url="https://example.com/functions/v1/print-agent",
        print_agent_api_key="test-api-key",
        workstation_id="ws-test-01",
        erp_enabled=True,
        erp_endpoints={
            "erp_box_fetch": ErpEndpointConfig(
                url="https://erp.example.com/boxes",
                method="GET",
                timeout_seconds=15,
            ),
            "completion_event": ErpEndpointConfig(
                url="https://erp.example.com/completion",
                method="POST",
                timeout_seconds=30,
            ),
        },
    )


class ErpAgentBoxFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        signal_patcher = patch.object(PrintAgent, "_setup_signal_handlers", autospec=True)
        self.addCleanup(signal_patcher.stop)
        signal_patcher.start()
        self.agent = PrintAgent(build_config())
        self.client = Mock()

    def test_box_fetch_forwards_payload_with_fixed_warehouse(self) -> None:
        request = GenericErpRequest(
            request_id="req-1",
            business_type="erp_box_fetch",
            endpoint_key="erp_box_fetch",
            request_payload={
                "quantity": 50,
                "client_id": "",
                "timestamp": "2026-03-31T15:38:12.357Z",
                "message_id": "Z000000022-0001::box-fetch::1774971492357",
                "requested_by": "4d67fef5-820a-4af0-9d83-7854c10ca68e",
                "warehouse_id": "WH-OTHER",
            },
            attempts=0,
            max_attempts=3,
        )

        with patch.object(
            self.agent,
            "_send_erp_http_request",
            return_value={
                "status_code": 200,
                "correlation_id": "corr-1",
                "idempotency_key": "req-1",
                "body": '{"status":"success","boxes":[{"box_id":"BOX-1","sscc":"","dm_code":""}]}',
            },
        ) as send_request:
            result = self.agent._process_erp_box_fetch(request, self.client)

        self.assertTrue(result)
        send_request.assert_called_once()
        _, kwargs = send_request.call_args
        self.assertEqual(
            kwargs["payload"],
            {
                "quantity": 50,
                "client_id": "",
                "timestamp": "2026-03-31T15:38:12.357Z",
                "message_id": "Z000000022-0001::box-fetch::1774971492357",
                "requested_by": "4d67fef5-820a-4af0-9d83-7854c10ca68e",
                "warehouse_id": "000093",
            },
        )
        self.client.post_success.assert_called_once_with(
            "req-1", [{"box_id": "BOX-1", "sscc": "", "dm_code": ""}]
        )
        self.client.post_failure.assert_not_called()

    def test_box_fetch_missing_payload_fails_locally(self) -> None:
        request = GenericErpRequest(
            request_id="req-2",
            business_type="erp_box_fetch",
            endpoint_key="erp_box_fetch",
            request_payload=None,
            attempts=0,
            max_attempts=3,
        )

        with patch.object(self.agent, "_send_erp_http_request") as send_request:
            result = self.agent._process_erp_box_fetch(request, self.client)

        self.assertFalse(result)
        send_request.assert_not_called()
        self.client.post_success.assert_not_called()
        self.client.post_failure.assert_called_once()
        failure_args, _ = self.client.post_failure.call_args
        self.assertEqual(failure_args[0], "req-2")
        self.assertIn("request_payload is missing or empty", failure_args[1])

    def test_completion_event_still_uses_exact_payload(self) -> None:
        payload = {
            "message_id": "msg-1",
            "invoice_id": "inv-1",
            "client_id": "client-1",
            "timestamp": "2026-03-31T15:38:12.357Z",
            "operator_id": "op-1",
            "warehouse_id": "WH-ORIGINAL",
            "boxes": [],
            "shortages": [],
            "summary": {},
        }
        request = GenericErpRequest(
            request_id="req-3",
            business_type="completion_event",
            endpoint_key="completion_event",
            request_payload=payload,
            attempts=0,
            max_attempts=3,
        )

        with patch.object(
            self.agent,
            "_send_erp_http_request",
            return_value={
                "status_code": 200,
                "correlation_id": "corr-3",
                "idempotency_key": "req-3",
                "body": '{"status":"success"}',
            },
        ) as send_request:
            result = self.agent._process_completion_event(request, self.client)

        self.assertTrue(result)
        send_request.assert_called_once()
        _, kwargs = send_request.call_args
        self.assertIs(kwargs["payload"], payload)
        self.client.post_completion_success.assert_called_once_with("req-3", {"status": "success"})
        self.client.post_completion_failure.assert_not_called()

    def test_completion_event_forwards_payload_without_required_field_validation(self) -> None:
        payload = {
            "message_id": "msg-2",
            "invoice_id": "inv-2",
            "timestamp": "2026-03-31T15:38:12.357Z",
            "operator_id": "op-2",
            "warehouse_id": "WH-ORIGINAL",
            "boxes": [],
            "shortages": [],
            "summary": {},
        }
        request = GenericErpRequest(
            request_id="req-4",
            business_type="completion_event",
            endpoint_key="completion_event",
            request_payload=payload,
            attempts=0,
            max_attempts=3,
        )

        with patch.object(
            self.agent,
            "_send_erp_http_request",
            return_value={
                "status_code": 200,
                "correlation_id": "corr-4",
                "idempotency_key": "req-4",
                "body": '{"status":"success"}',
            },
        ) as send_request:
            result = self.agent._process_completion_event(request, self.client)

        self.assertTrue(result)
        send_request.assert_called_once()
        _, kwargs = send_request.call_args
        self.assertIs(kwargs["payload"], payload)
        self.client.post_completion_success.assert_called_once_with("req-4", {"status": "success"})
        self.client.post_completion_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
