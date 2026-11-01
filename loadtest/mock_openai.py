"""A tiny OpenAI-compatible model service, for load tests and offline demos.

The application itself has no fake model: it always talks to a real
OpenAI-compatible endpoint. When you want to stress the stack (or click through
the UI on a laptop with no key and no network), point the app at this process —
`MODEL_BASE_URL=http://localhost:7800/v1` — and it behaves like a model server:

    GET  /v1/models                 -> {"data": [{"id": "loadtest-model"}, ...]}
    POST /v1/chat/completions       -> a tool call for `issue_refund`, then prose

It speaks the same protocol the app speaks, so the code path under test is the
real one; only the model is replaced. Uses only the standard library.

    python loadtest/mock_openai.py --port 7800
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "loadtest-model"
_ORDER_ID = re.compile(r"\b[A-Z]{2,}-\d[\w-]*\b")
# Any 退/refund phrasing in the mock triggers a refund attempt; the real app's
# policy gate decides the outcome, which is the point of the load test.
_REFUND_WORDS = ("refund", "退")

REPLY = "我已经为你处理好了，详细信息可以查看上一步的结果。"


def _completion(payload: dict) -> dict:
    """Call the right tool for the ask, else answer in prose."""
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    text = str((last_user or {}).get("content") or "")
    handled = any(m.get("role") == "tool" for m in messages)
    lowered = text.lower()
    order_ids = list(dict.fromkeys(_ORDER_ID.findall(text)))
    wants_refund = any(word in lowered for word in _REFUND_WORDS)

    if handled or not order_ids or not tools:
        return {"role": "assistant", "content": REPLY}

    # A tool-capable model always answers a data question with a tool call. Only
    # `issue_refund` when a refund is actually being asked for; otherwise fall
    # back to a read-only tool (that is what the connection probe sends).
    names = [t.get("function", {}).get("name", "") for t in tools]
    if wants_refund and "issue_refund" in names:
        targets = order_ids
        tool = "issue_refund"
    elif "get_order" in names:
        targets = order_ids[:1]
        tool = "get_order"
    else:
        targets = order_ids[:1]
        tool = names[0]

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps({"order_id": order_id})},
            }
            for order_id in targets
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON"}})
            return
        message = _completion(payload)
        prompt_tokens = sum(len(str(m.get("content") or "")) // 4 for m in payload.get("messages") or [])
        self._send(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 20,
                    "total_tokens": prompt_tokens + 20,
                },
            },
        )

    def log_message(self, *args) -> None:  # keep the console quiet
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7800)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock OpenAI-compatible service on http://{args.host}:{args.port}/v1 "
          f"(model: {MODEL_ID})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
