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


DEFAULT_HTTP_TIMEOUT_SECONDS = 10
DEFAULT_CALLBACK_RETRIES = 3


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
                logging.info("[ERP-AGENT] INCOMING AGENT REQUEST business_type=%s request_id=%s: %s",
                           business_type, req.get("id"), json.dumps(req, ensure_ascii=False))

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
                    logging.info("[ERP-AGENT] Request payload: %s", json.dumps(sanitized_payload, ensure_ascii=False))
                else:
                    logging.info("[ERP-AGENT] Request payload: %s", str(payload)[:500])

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

                logging.info("[ERP-AGENT] Final request body: %s", request_kwargs["data"])

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
            logging.info("[ERP-AGENT] PAYLOAD: %s", json.dumps(erp_payload, ensure_ascii=False))
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
            logging.info("[ERP-AGENT] boxes: %s", json.dumps(normalized_boxes, ensure_ascii=False))
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
        logging.info("    request_payload: %s", json.dumps(request.request_payload, ensure_ascii=False)[:500])
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
        logging.info("[ERP-AGENT] PAYLOAD: %s", json.dumps(erp_payload, ensure_ascii=False))
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
            logging.info("[ERP-AGENT] response: %s", json.dumps(response_body, ensure_ascii=False))
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


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env file if present."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            print(f"Loading env var from .env: {key.strip()}={value.strip()}")
            # os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            os.environ[key.strip()] = value.strip().strip('"').strip("'")

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
    pprint.pprint(config)
    return config


PID_FILE = "print_agent.pid"


def write_pid_file() -> None:
    """Write current process ID to PID file."""
    pid = os.getpid()
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
        logging.info("PID file created: %s (PID=%d)", PID_FILE, pid)
    except Exception as exc:
        logging.warning("Could not create PID file: %s", exc)


def remove_pid_file() -> None:
    """Remove PID file on clean shutdown."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            logging.info("PID file removed: %s", PID_FILE)
    except Exception as exc:
        logging.warning("Could not remove PID file: %s", exc)


def check_pid_file() -> bool:
    """Check if another instance is already running.

    Returns True if another instance is running, False otherwise.
    Also cleans up stale PID files (>1 hour old) to prevent issues after crashes.
    """
    if not os.path.exists(PID_FILE):
        return False

    try:
        # Check if PID file is stale (crashed process left PID file behind)
        import time
        pid_file_age = time.time() - os.path.getmtime(PID_FILE)
        if pid_file_age > 3600:  # 1 hour
            logging.warning("Found stale PID file (age=%d seconds) - removing", pid_file_age)
            try:
                os.remove(PID_FILE)
                logging.info("Stale PID file removed")
            except Exception as exc:
                logging.warning("Could not remove stale PID file: %s", exc)
            return False

        with open(PID_FILE, "r", encoding="utf-8") as f:
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

    if daemon:
        # Background mode: log to file
        log_file = os.getenv("LOG_FILE", "print_agent.log")
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=log_file,
            filemode="a",  # Append mode
        )
        logging.info("Background mode started - logging to file: %s", log_file)
    else:
        # Foreground mode: log to console
        logging.basicConfig(
            level=log_level,
            format=log_format,
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
    args = parser.parse_args()

    # Handle --stop command
    if args.stop:
        if not os.path.exists(PID_FILE):
            print("No PID file found - agent may not be running")
            sys.exit(1)

        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
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
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                print(f"Print agent is running (PID={pid})")
            except Exception:
                print("Print agent status unknown (PID file exists but unreadable)")
        else:
            print("Print agent is not running")
        return

    # Check for existing instance
    if check_pid_file():
        print("ERROR: Another instance is already running!")
        print("Use --stop to stop the existing instance first")
        sys.exit(1)

    # Setup logging based on mode
    setup_logging(daemon=args.daemon)

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

        # Run print loop in main thread
        agent.run_forever()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as exc:
        logging.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
