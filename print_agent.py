#!/usr/bin/env python3
"""On-prem print agent for Zebra/compatible network printers.

This agent polls `print_jobs` from Supabase/PostgREST, sends ZPL to printers via
raw TCP 9100, and marks jobs as completed/failed.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests


@dataclass
class Config:
    supabase_url: str
    supabase_key: str
    print_agent_callback_url: str
    print_agent_api_key: str
    poll_interval_seconds: float = 2.0
    printer_port: int = 9100
    printer_timeout_seconds: float = 5.0


class PrintAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = f"{config.supabase_url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": config.supabase_key,
            "Authorization": f"Bearer {config.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self.callback_headers = {
            "X-API-Key": config.print_agent_api_key,
            "Content-Type": "application/json",
        }

    def run_forever(self) -> None:
        logging.info("print-agent started")
        while True:
            try:
                processed = self.process_one()
                if not processed:
                    time.sleep(self.config.poll_interval_seconds)
            except Exception as exc:  # broad by design for resilient loop
                logging.exception("loop error: %s", exc)
                time.sleep(self.config.poll_interval_seconds)

    def _fetch_pending_job(self) -> Optional[Dict[str, Any]]:
        params = {
            "select": "id,zpl,printer_ip,status,created_at",
            "status": "eq.pending",
            "order": "created_at.asc",
            "limit": "1",
        }
        response = requests.get(
            f"{self.base_url}/print_jobs",
            headers=self.headers,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def _claim_job(self, job_id: str) -> bool:
        params = {
            "id": f"eq.{job_id}",
            "status": "eq.pending",
            "select": "id",
        }
        payload = {
            "status": "processing",
            "processing_started_at": datetime.now(timezone.utc).isoformat(),
        }
        response = requests.patch(
            f"{self.base_url}/print_jobs",
            headers=self.headers,
            params=params,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        claimed_rows = response.json()
        return len(claimed_rows) == 1

    def _send_to_printer(self, job: Dict[str, Any]) -> None:
        printer_ip = job.get("printer_ip")
        zpl = job.get("zpl")
        if not printer_ip or not zpl:
            raise ValueError("job missing printer_ip or zpl")

        with socket.create_connection(
            (printer_ip, self.config.printer_port),
            timeout=self.config.printer_timeout_seconds,
        ) as sock:
            sock.sendall(zpl.encode("utf-8"))

    def _mark_done(self, job_id: str) -> None:
        self._notify_print_service(job_id=job_id, status="completed", error_message=None)

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        self._notify_print_service(
            job_id=job_id,
            status="failed",
            error_message=error_message[:500],
        )

    def _notify_print_service(
        self,
        job_id: str,
        status: str,
        error_message: Optional[str],
    ) -> None:
        payload = {
            "jobId": job_id,
            "status": status,
            "errorMessage": error_message,
        }
        response = requests.post(
            self.config.print_agent_callback_url,
            headers=self.callback_headers,
        self._update_job(job_id, {"status": "completed", "error": None})

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        self._update_job(job_id, {"status": "failed", "error": error_message[:500]})

    def _update_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        params = {"id": f"eq.{job_id}", "select": "id"}
        response = requests.patch(
            f"{self.base_url}/print_jobs",
            headers=self.headers,
            params=params,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()

    def process_one(self) -> bool:
        """Process one job for tests/local validation."""
        job = self._fetch_pending_job()
        if not job:
            return False

        job_id = job["id"]
        if not self._claim_job(job_id):
            return False

        logging.info("processing job=%s", job_id)
        try:
            self._send_to_printer(job)
        except Exception as exc:
            self._mark_failed(job_id, str(exc))
            raise
        else:
            self._mark_done(job_id)
            logging.info("completed job=%s", job_id)
            return True


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
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> Config:
    load_dotenv()

    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    print_agent_callback_url = os.environ["PRINT_AGENT_CALLBACK_URL"]
    print_agent_api_key = os.environ["PRINT_AGENT_API_KEY"]

    return Config(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        print_agent_callback_url=print_agent_callback_url,
        print_agent_api_key=print_agent_api_key,
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "2")),
        printer_port=int(os.getenv("PRINTER_PORT", "9100")),
        printer_timeout_seconds=float(os.getenv("PRINTER_TIMEOUT_SECONDS", "5")),
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    agent = PrintAgent(load_config())
    agent.run_forever()


if __name__ == "__main__":
    main()
