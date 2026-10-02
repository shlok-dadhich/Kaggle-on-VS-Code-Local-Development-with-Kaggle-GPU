"""Session expiration and connectivity handling for Kaggle runner tools.

Provides:
- Token redaction for safe logging, JSON output, and tracebacks
- Session liveness probing
- Guarded call wrapper for remote requests
- Exit code constants
"""

import os
import re
import sys
import threading
import time
import traceback
from typing import Callable, Optional

import requests


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_EXPIRED = 3
EXIT_OFFLINE = 4

TOKEN_URL_PATTERN = re.compile(r'(/k/[^/]+/)([^/]+)(/proxy)')
TOKEN_QUERY_PATTERN = re.compile(r'(token=)([^&\s]+)')
PROXY_URL_PATTERN = re.compile(r'(https?://[^\s\"\'\\]*kaggle\.net/k/[^/\s\\]+/)([^/\s\\]+)(/proxy[^\s\"\'\\]*)')


def redact(text: str, token: Optional[str] = None) -> str:
    """Redact token from text for safe display and logging."""
    if not text:
        return text

    result = str(text)
    result = TOKEN_URL_PATTERN.sub(r'\1<redacted>\3', result)
    result = TOKEN_QUERY_PATTERN.sub(r'\1<redacted>', result)
    result = PROXY_URL_PATTERN.sub(r'\1<redacted>\3', result)

    if token is not None and token:
        result = result.replace(token, "<redacted>")

    return result


def format_exception(exc: Exception, token: Optional[str] = None) -> str:
    """Format an exception for CLI output without leaking a session token."""
    return redact(f"{type(exc).__name__}: {exc}", token)


def format_traceback(exc: Exception, token: Optional[str] = None) -> str:
    """Format and redact a traceback for opt-in debug output."""
    return redact(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        token,
    )


def cli_main(main_fn: Callable, argv=None):
    """Run a CLI entry point with one redacted, traceback-free error boundary."""
    try:
        return main_fn(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {format_exception(exc)}", file=sys.stderr)
        if os.getenv("KAGGLE_RUNNER_DEBUG") == "1":
            print(format_traceback(exc), file=sys.stderr)
        raise SystemExit(EXIT_FAILURE) from None


def log(*args, token: Optional[str] = None, **kwargs):
    """Safe logging with automatic token redaction."""
    args = [redact(str(arg), token) for arg in args]
    print(*args, **kwargs)


class SessionExpired(Exception):
    """Raised when Kaggle session is confirmed expired."""
    pass


class OfflineState:
    """Tracks offline/online state with exponential backoff."""

    def __init__(self):
        self._offline = False
        self._backoff = 2.0
        self._max_backoff = 60.0
        self._lock = threading.Lock()
        self._printed_offline = False
        self._printed_online = False

    @property
    def offline(self) -> bool:
        with self._lock:
            return self._offline

    def mark_offline(self) -> None:
        with self._lock:
            if not self._offline:
                self._offline = True
                self._printed_offline = False
                self._printed_online = False
            if not self._printed_offline:
                self._printed_offline = True
                self._printed_online = False

    def mark_online(self) -> None:
        with self._lock:
            if self._offline:
                self._offline = False
                self._backoff = 2.0
                self._printed_online = False
                self._printed_offline = False
            if not self._printed_online:
                self._printed_online = True
                self._printed_offline = False

    def get_backoff(self) -> float:
        with self._lock:
            if not self._offline:
                return 0.0
            backoff = self._backoff
            self._backoff = min(self._backoff * 1.5, self._max_backoff)
            return backoff


OFFLINE_STATE = OfflineState()


def _extract_status(error: Exception) -> Optional[int]:
    response = getattr(error, 'response', None)
    if response is not None:
        status = getattr(response, 'status_code', None)
        if isinstance(status, int):
            return status
    if hasattr(error, 'status_code'):
        status = getattr(error, 'status_code', None)
        if isinstance(status, int):
            return status
    return None


def is_auth_error(error: Exception) -> bool:
    status = _extract_status(error)
    return status in (401, 403)


def _is_offline_error(error: Exception) -> bool:
    if isinstance(error, (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectionError,
    )):
        return True
    resp = getattr(error, 'response', None)
    if resp is not None:
        status = getattr(resp, 'status_code', None)
        if isinstance(status, int) and status >= 500:
            return True
    return False


def probe(client) -> str:
    """Probe session liveness.

    Returns:
        'alive' - session is valid
        'expired' - session expired (401/403)
        'offline' - network/connectivity issue
    """
    base = getattr(client, 'base_url', None)
    if not base:
        return 'offline'

    # Probe /api
    try:
        resp = client.session.get(f"{base}/api", timeout=10)
        if resp.status_code in (401, 403):
            return 'expired'
        if resp.status_code >= 500:
            return 'offline'
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ConnectionError):
        return 'offline'

    # Probe /api/kernels
    try:
        resp = client.session.get(f"{base}/api/kernels", timeout=10)
        if resp.status_code in (401, 403):
            return 'expired'
        if resp.status_code >= 500:
            return 'offline'
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ConnectionError):
        return 'offline'

    return 'alive'


def confirm_state(client, max_tries: int = 3, delay: float = 1.0) -> str:
    """Confirm session state with multiple probes."""
    expired_count = 0
    offline_count = 0

    for _ in range(max_tries):
        state = probe(client)
        if state == 'expired':
            expired_count += 1
        elif state == 'offline':
            offline_count += 1
        else:
            return 'alive'
        time.sleep(delay)

    if expired_count == max_tries:
        return 'expired'
    if offline_count > 0:
        return 'offline'
    return 'expired'


SESSION_DEAD = threading.Event()


def log_session_expired():
    """Print session expired notice."""
    print()
    print("═" * 60)
    print("Kaggle session expired or was stopped.")
    print("═" * 60)
    print()
    print("1) Start a new Kaggle session")
    print("2) Copy the new Jupyter URL")
    print("3) Run: kaggle-sync")
    print()
    print("Local edits made after the last sync will upload")
    print("automatically on restart.")
    print("═" * 60)
    print()


def guarded_call(client, fn: Callable, *args, **kwargs):
    """Execute a request with session and offline guarding."""
    while True:
        try:
            res = fn(*args, **kwargs)
            OFFLINE_STATE.mark_online()
            return res
        except Exception as e:
            if is_auth_error(e):
                state = confirm_state(client)
                if state == 'expired':
                    if not SESSION_DEAD.is_set():
                        SESSION_DEAD.set()
                        log_session_expired()
                    raise SessionExpired("Kaggle session expired") from e
                elif state == 'offline':
                    OFFLINE_STATE.mark_offline()
                    continue
                continue

            if _is_offline_error(e):
                OFFLINE_STATE.mark_offline()
                backoff = OFFLINE_STATE.get_backoff()
                if backoff > 0:
                    log(f"[OFFLINE] cannot reach Kaggle - retrying in {backoff:.0f}s (edits are kept)")
                    time.sleep(backoff)
                    continue
                else:
                    continue

            status = _extract_status(e)
            if status == 404:
                raise

            raise