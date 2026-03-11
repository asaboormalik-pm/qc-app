#!/usr/bin/env python3
"""On-prem print agent for Zebra/compatible network printers.

This agent polls an edge function for print jobs, sends ZPL to printers via raw
TCP 9100, and posts completion or failure back to the same edge function.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


DEFAULT_HTTP_TIMEOUT_SECONDS = 10


@dataclass
class Config:
    print_agent_url: str
    print_agent_api_key: str
    poll_interval_seconds: float = 2.0
    printer_port: int = 9100
    printer_timeout_seconds: float = 5.0


class PrintAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.headers = {
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
        response = requests.get(
            self.config.print_agent_url,
            headers=self.headers,
            params={"action": "poll", "limit": "1"},
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        jobs: List[Dict[str, Any]]
        if isinstance(body, dict):
            jobs = body.get("jobs") or []
        elif isinstance(body, list):
            jobs = body
        else:
            raise ValueError("Unexpected poll response format from print agent endpoint")
        return jobs[0] if jobs else None

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
            self.config.print_agent_url,
            headers=self.headers,
            json=payload,
            timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    def process_one(self) -> bool:
        """Process one job for tests/local validation."""
        job = self._fetch_pending_job()
        if not job:
            return False

        job_id = job["id"]
        logging.info("processing job=%s printer_ip=%s", job_id, job.get("printer_ip"))
        try:
            self._send_to_printer(job)
        except Exception as exc:
            self._mark_failed(job_id, str(exc))
            raise

        try:
            self._mark_done(job_id)
        except Exception as exc:
            logging.error(
                "completion callback failed after successful print job=%s error=%s",
                job_id,
                exc,
            )
            raise

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


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    load_dotenv()

    return Config(
        print_agent_url=require_env("PRINT_AGENT_CALLBACK_URL"),
        print_agent_api_key=require_env("PRINT_AGENT_API_KEY"),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "2")),
        printer_port=int(os.getenv("PRINTER_PORT", "9100")),
        printer_timeout_seconds=float(os.getenv("PRINTER_TIMEOUT_SECONDS", "5")),
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    agent = PrintAgent(load_config())
    agent.run_forever()


if __name__ == "__main__":
    main()
