"""Shared Jupyter kernel websocket execution helper.

Used by both kaggle_runner.run (KaggleClient.execute) and sync.py
(JupyterClient.execute) so long-running cells survive:

* silent periods longer than the socket timeout (short-poll recv +
  periodic ws.ping() keepalive instead of failing),
* proxy / idle websocket drops (reconnect with the SAME session_id,
  without resending execute_request),
* long runs (no default deadline; Kaggle sessions last up to 12 h).

Only depends on ``requests`` and ``websocket-client``.
"""

import json
import os
import re
import ssl
import time
import uuid
from typing import Callable, Optional, Tuple

import requests
import websocket

from . import session_guard


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_RE.sub("", text)


def _is_timeout_error(error: Exception) -> bool:
    return isinstance(error, websocket.WebSocketTimeoutException)


def _is_connection_error(error: Exception) -> bool:
    return isinstance(
        error,
        (
            websocket.WebSocketConnectionClosedException,
            ConnectionError,
            BrokenPipeError,
            ssl.SSLError,
            OSError,
        ),
    )


def _connect(ws_url: str, token: str, http_base: str, poll_interval: float):
    return websocket.create_connection(
        ws_url,
        header=[f"Authorization: token {token}"],
        origin=http_base,
        timeout=poll_interval,
    )


def _close_quietly(ws):
    try:
        if ws is not None:
            ws.close()
    except Exception:
        pass


def _kernel_execution_state(
    http_base: str,
    token: str,
    kernel_id: str,
    poll_interval: float,
) -> Tuple[Optional[int], Optional[str]]:
    """Return (http_status, execution_state or None).

    Returns (None, None) when the check itself cannot reach the
    server (network error); the caller then retries the reconnect
    anyway instead of assuming the session is gone.
    """
    try:
        response = requests.get(
            f"{http_base}/api/kernels/{kernel_id}",
            headers={"Authorization": f"token {token}"},
            timeout=poll_interval,
        )
    except (ConnectionError, OSError):
        return None, None
    except Exception:
        return None, None

    state = None
    if response.status_code == 200:
        try:
            state = response.json().get("execution_state")
        except Exception:
            state = None
    return response.status_code, state


def execute_in_kernel(
    ws_base: str,
    http_base: str,
    token: str,
    kernel_id: str,
    code: str,
    *,
    on_text: Callable[[str], None],
    user_expressions: Optional[dict] = None,
    timeout: Optional[float] = None,
    poll_interval: float = 5.0,
    ping_interval: float = 20.0,
    max_reconnects: int = 8,
) -> dict:
    """Execute code in a remote Jupyter kernel over websocket.

    Returns a dict with keys: status ("ok" / "error" / "aborted" /
    "lost"), user_expressions (dict), error_text (str),
    missed_output (bool).

    * ``timeout`` is the overall limit in seconds; None means no
      deadline. When ``timeout`` is None, the ``KAGGLE_RUN_TIMEOUT``
      environment variable (seconds) is honoured if set.
    * ``poll_interval`` is the socket timeout for each ``recv()``
      (short poll, NOT the overall limit).
    * ``ping_interval`` controls how often ``ws.ping()`` is sent
      while waiting silently, keeping idle connections alive.
    """

    if timeout is None:
        env_timeout = os.getenv("KAGGLE_RUN_TIMEOUT", "")
        env_timeout = env_timeout.strip()
        if env_timeout:
            try:
                timeout = float(env_timeout)
            except ValueError:
                timeout = None

    if timeout is not None:
        deadline = time.time() + timeout
    else:
        deadline = None

    session_id = uuid.uuid4().hex
    msg_id = uuid.uuid4().hex

    ws_url = f"{ws_base}/api/kernels/{kernel_id}/channels?session_id={session_id}"

    message = {
        "header": {
            "msg_id": msg_id,
            "username": "kaggle-runner",
            "session": session_id,
            "msg_type": "execute_request",
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": False,
            "user_expressions": dict(user_expressions or {}),
            "allow_stdin": False,
            "stop_on_error": True,
        },
        "channel": "shell",
    }

    ws = _connect(ws_url, token, http_base, poll_interval)

    # The request is sent exactly once for the whole call,
    # including across reconnects.
    ws.send(json.dumps(message))

    had_error = False
    error_text = ""
    user_expression_results = {}
    seen_reply = False
    reconnects = 0
    last_ping = time.time()

    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                return {
                    "status": "aborted",
                    "user_expressions": dict(user_expression_results),
                    "error_text": error_text or "Execution timed out.",
                    "missed_output": False,
                }

            try:
                raw = ws.recv()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                needs_reconnect = False

                if _is_timeout_error(error):
                    # Not an error: nothing arrived within the
                    # short poll window. Keep the connection
                    # alive with periodic pings.
                    now = time.time()
                    if now - last_ping >= ping_interval:
                        try:
                            ws.ping()
                        except KeyboardInterrupt:
                            raise
                        except Exception as ping_error:
                            if _is_connection_error(ping_error):
                                needs_reconnect = True
                            # Any other ping failure is ignored; just poll again.
                        if not needs_reconnect:
                            last_ping = time.time()
                            continue
                    else:
                        continue

                elif _is_connection_error(error):
                    needs_reconnect = True

                if needs_reconnect:
                    # Connection dropped (or ping proved it
                    # dead): reconnect with the SAME session_id
                    # and kernel_id, without resending the
                    # execute_request.
                    reconnects += 1

                    if reconnects > max_reconnects:
                        return {
                            "status": "lost",
                            "user_expressions": dict(user_expression_results),
                            "error_text": (
                                error_text
                                or "Lost connection to the "
                                "Kaggle kernel (reconnects "
                                "exhausted)."
                            ),
                            "missed_output": True,
                        }

                    delay = min(2 ** (reconnects - 1), 30)

                    # KeyboardInterrupt during backoff must
                    # propagate untouched.
                    time.sleep(delay)

                    http_status, execution_state = _kernel_execution_state(
                        http_base, token, kernel_id, poll_interval
                    )

                    if http_status == 404:
                        return {
                            "status": "lost",
                            "user_expressions": dict(user_expression_results),
                            "error_text": (
                                "Kaggle session ended "
                                "(kernel not found)."
                            ),
                            "missed_output": False,
                        }

                    _close_quietly(ws)

                    try:
                        ws = _connect(ws_url, token, http_base, poll_interval)
                    except KeyboardInterrupt:
                        raise
                    except Exception as connect_error:
                        if _is_connection_error(connect_error) or _is_timeout_error(connect_error):
                            # Back off again (counts as the
                            # next reconnect iteration).
                            reconnects += 1
                            if reconnects > max_reconnects:
                                _close_quietly(ws)
                                return {
                                    "status": "lost",
                                    "user_expressions": dict(
                                        user_expression_results
                                    ),
                                    "error_text": (
                                        error_text
                                        or "Lost connection to "
                                        "the Kaggle kernel "
                                        "(reconnects "
                                        "exhausted)."
                                    ),
                                    "missed_output": True,
                                }
                            delay = min(2 ** (reconnects - 1), 30)
                            time.sleep(delay)
                            continue
                        raise

                    last_ping = time.time()

                    if execution_state == "idle" and not seen_reply:
                        print(
                            "Warning: reconnected to an idle "
                            "kernel; some output may be "
                            "missing.",
                            flush=True,
                        )
                        return {
                            "status": (
                                "error"
                                if had_error
                                else "ok"
                            ),
                            "user_expressions": dict(
                                user_expression_results
                            ),
                            "error_text": error_text,
                            "missed_output": True,
                        }

                    continue

                raise

            if not raw:
                continue

            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue

            if not isinstance(data, dict):
                continue

            parent = data.get("parent_header", {})
            if not isinstance(parent, dict):
                continue

            # Only handle messages for our request.
            if parent.get("msg_id") != msg_id:
                continue

            header = data.get("header", {})
            if not isinstance(header, dict):
                continue

            msg_type = header.get("msg_type")
            content = data.get("content", {})

            if not isinstance(content, dict):
                content = {}

            if msg_type == "stream":
                on_text(session_guard.redact(content.get("text", ""), token))

            elif msg_type in ("execute_result", "display_data"):
                message_data = content.get("data", {})
                if isinstance(message_data, dict):
                    if "text/plain" in message_data:
                        on_text(
                            session_guard.redact(
                                message_data["text/plain"],
                                token,
                            )
                        )

            elif msg_type == "error":
                had_error = True
                traceback = content.get("traceback", [])
                if isinstance(traceback, list):
                    cleaned = "\n".join(
                        session_guard.redact(_strip_ansi(line), token)
                        for line in traceback
                    )
                else:
                    cleaned = session_guard.redact(
                        _strip_ansi(traceback),
                        token,
                    )
                if cleaned:
                    if error_text:
                        error_text += "\n"
                    error_text += cleaned

            elif msg_type == "execute_reply":
                seen_reply = True
                reply_status = content.get("status", "ok")
                expressions = content.get("user_expressions", {})
                if isinstance(expressions, dict):
                    user_expression_results = dict(expressions)
                if reply_status == "error":
                    had_error = True
                    if not error_text:
                        reply_traceback = content.get("traceback", [])
                        if reply_traceback:
                            if isinstance(reply_traceback, list):
                                error_text = "\n".join(
                                    session_guard.redact(
                                        _strip_ansi(line),
                                        token,
                                    )
                                    for line in reply_traceback
                                )
                            else:
                                error_text = session_guard.redact(
                                    _strip_ansi(reply_traceback),
                                    token,
                                )
                        else:
                            name = content.get("ename", "")
                            value = content.get("evalue", "")
                            error_text = session_guard.redact(
                                f"{name}: {value}".strip(": "),
                                token,
                            )

                # Finish on execute_reply. Do NOT rely on
                # status:idle alone (it may arrive early or
                # belong to another channel).
                return {
                    "status": "error" if had_error else "ok",
                    "user_expressions": dict(user_expression_results),
                    "error_text": error_text,
                    "missed_output": False,
                }

            # All other message types (status, etc.) are
            # intentionally ignored here.

    finally:
        _close_quietly(ws)