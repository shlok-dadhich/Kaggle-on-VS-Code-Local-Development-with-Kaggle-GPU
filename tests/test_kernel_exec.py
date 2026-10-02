"""Websocket output formatting and reconnect recovery tests."""

import json

import websocket

from kaggle_runner import kernel_exec


def _message(msg_id, msg_type, content):
    return json.dumps({
        "parent_header": {"msg_id": msg_id},
        "header": {"msg_type": msg_type},
        "content": content,
    })


class _Socket:
    def __init__(self, mode, sent_messages):
        self.mode = mode
        self.sent_messages = sent_messages
        self.messages = []

    def send(self, raw):
        request = json.loads(raw)
        self.sent_messages.append(request)
        if self.mode == "stream":
            msg_id = request["header"]["msg_id"]
            self.messages = [
                _message(
                    msg_id,
                    "stream",
                    {"text": "\x1b[31mstep 1\rstep 2\x1b[0m"},
                ),
                _message(
                    msg_id,
                    "execute_reply",
                    {"status": "ok", "user_expressions": {}},
                ),
            ]
        elif self.mode == "reply":
            msg_id = request["header"]["msg_id"]
            expressions = {}
            for name in request["content"]["user_expressions"]:
                expressions[name] = {
                    "data": {"text/plain": "5"},
                }
            self.messages = [
                _message(
                    msg_id,
                    "execute_reply",
                    {"status": "ok", "user_expressions": expressions},
                ),
            ]

    def recv(self):
        if self.mode == "closed":
            raise websocket.WebSocketConnectionClosedException("socket closed")
        return self.messages.pop(0)

    def ping(self):
        return None

    def close(self):
        return None


def test_stream_output_strips_ansi_but_keeps_carriage_returns(monkeypatch):
    sent = []
    monkeypatch.setattr(
        kernel_exec.websocket,
        "create_connection",
        lambda *args, **kwargs: _Socket("stream", sent),
    )
    output = []

    result = kernel_exec.execute_in_kernel(
        "ws://example",
        "http://example",
        "token",
        "kernel-id",
        "print('progress')",
        on_text=output.append,
    )

    assert result["status"] == "ok"
    assert output == ["step 1\rstep 2"]


def test_idle_reconnect_probes_result_without_resending_original(
    monkeypatch,
):
    sent = []
    sockets = iter((
        _Socket("closed", sent),
        _Socket("idle", sent),
        _Socket("reply", sent),
    ))
    monkeypatch.setattr(
        kernel_exec.websocket,
        "create_connection",
        lambda *args, **kwargs: next(sockets),
    )
    monkeypatch.setattr(
        kernel_exec,
        "_kernel_execution_state",
        lambda *args, **kwargs: (200, "idle"),
    )
    monkeypatch.setattr(kernel_exec.time, "sleep", lambda _seconds: None)

    result = kernel_exec.execute_in_kernel(
        "ws://example",
        "http://example",
        "token",
        "kernel-id",
        "run original script",
        on_text=lambda _text: None,
    )

    assert result["status"] == "ok"
    assert result["user_expressions"]["rc"]["data"]["text/plain"] == "5"
    assert [item["content"]["code"] for item in sent] == [
        "run original script",
        "pass",
    ]


def test_idle_reconnect_without_recoverable_rc_is_unknown(monkeypatch):
    sent = []
    sockets = iter((
        _Socket("closed", sent),
        _Socket("idle", sent),
        _Socket("reply", sent),
    ))
    monkeypatch.setattr(
        kernel_exec.websocket,
        "create_connection",
        lambda *args, **kwargs: next(sockets),
    )
    monkeypatch.setattr(
        kernel_exec,
        "_kernel_execution_state",
        lambda *args, **kwargs: (200, "idle"),
    )
    monkeypatch.setattr(kernel_exec.time, "sleep", lambda _seconds: None)

    def reply_without_expression(raw):
        request = json.loads(raw)
        msg_id = request["header"]["msg_id"]
        return _message(
            msg_id,
            "execute_reply",
            {"status": "ok", "user_expressions": {}},
        )

    original_send = _Socket.send

    def send_without_rc(self, raw):
        original_send(self, raw)
        if self.mode == "reply":
            self.messages = [reply_without_expression(raw)]

    monkeypatch.setattr(_Socket, "send", send_without_rc)
    result = kernel_exec.execute_in_kernel(
        "ws://example",
        "http://example",
        "token",
        "kernel-id",
        "run original script",
        on_text=lambda _text: None,
    )

    assert result["status"] == "unknown"
    assert "run result unknown" in result["error_text"]
