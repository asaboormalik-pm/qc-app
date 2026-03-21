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
import pprint
import threading
import atexit
from dataclasses import dataclass
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


@dataclass
class Config:
    print_agent_url: str
    print_agent_api_key: str
    poll_interval_seconds: float = 2.0
    printer_port: int = 9100
    printer_timeout_seconds: float = 5.0
    max_concurrent_jobs: int = 3  # Support 2-3 printers per station
    workstation_id: str = ""  # Unique identifier for this workstation (for tracking)


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
                                    self._mark_failed(job.get("id"), str(exc))
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
        return jobs

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
        return jobs[0] if jobs else None

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
        # Log ZPL at DEBUG level with truncation for very long payloads
        zpl_preview = zpl[:200] + "..." if len(zpl) > 200 else zpl
        logging.debug("ZPL data for job=%s:\n%s", job.get("id"), zpl_preview)

        try:
            # Use context manager for guaranteed socket cleanup
            with socket.create_connection(
                (printer_ip, printer_port),
                timeout=self.config.printer_timeout_seconds,
            ) as sock:
                sock.sendall(zpl.encode("utf-8"))
            logging.info("Label sent to printer at %s:%s", printer_ip, printer_port)
        except socket.timeout:
            raise Exception(f"Connection timeout to printer {printer_ip}:{printer_port}")
        except ConnectionRefusedError:
            raise Exception(f"Connection refused by printer {printer_ip}:{printer_port}")
        except OSError as e:
            raise Exception(f"Network error communicating with printer {printer_ip}:{printer_port}: {e}")

    def _mark_done(self, job_id: str) -> None:
        self._notify_print_service(job_id=job_id, status="completed", error_message=None)

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        self._notify_print_service(
            job_id=job_id,
            status="failed",
            error_message=(error_message[:500] if error_message else None),
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
        try:
            response = requests.post(
                self.config.print_agent_url,
                headers=self.headers,
                json=payload,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
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
        thread_id = threading.current_thread().name
        logging.info("[%s] processing job=%s printer_ip=%s printer_port=%s",
                     thread_id, job_id, job.get("printer_ip"), job.get("printer_port"))
        try:
            self._send_to_printer(job)
        except Exception as exc:
            self._mark_failed(job_id, str(exc))
            logging.error("[%s] failed job=%s error=%s", thread_id, job_id, exc)
            return False

        try:
            self._mark_done(job_id)
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

    config = Config(
        print_agent_url=require_env("PRINT_AGENT_CALLBACK_URL"),
        print_agent_api_key=require_env("PRINT_AGENT_API_KEY"),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "2")),
        printer_port=int(os.getenv("PRINTER_PORT", "9100")),
        printer_timeout_seconds=float(os.getenv("PRINTER_TIMEOUT_SECONDS", "5")),
        max_concurrent_jobs=max_jobs,
        workstation_id=workstation_id,
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
        agent.run_forever()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as exc:
        logging.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()


