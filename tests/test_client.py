"""The SDK's retries and wait_ready() deadline, against fake transports and a slow server."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from msgboard.client import BoardError, Client


def _answers(*responses):
    """A transport that gives `responses` in turn (the last one again once they run out), and counts calls."""
    calls = []

    def handler(request):
        calls.append(request)
        status, code = responses[min(len(calls), len(responses)) - 1]
        if status >= 400:
            return httpx.Response(status, headers={"Retry-After": "0"},
                                  json={"error": {"code": code, "message": "retry shortly"}})
        return httpx.Response(status, json={"id": 1, "seq": 1, "body": "hello"})

    return httpx.MockTransport(handler), calls


def test_a_restarting_board_is_retried_past_max_retries(tmp_path):
    # Review #5 #7: 5 retries of `Retry-After: 2` gave up after about 10-15 s of 503s, while a Space
    # restart on a slow bucket refuses writes for longer.
    transport, calls = _answers(*[(503, "shutting_down")] * 8, *[(503, "starting")] * 4, (201, None))
    with Client("http://board.test", "t", transport=transport, backoff=0.001) as c:
        assert c.post(1, "hello")["seq"] == 1
    assert len(calls) == 13


def test_restart_retries_stop_after_restart_timeout():
    transport, calls = _answers((503, "shutting_down"))
    with Client("http://board.test", "t", transport=transport, backoff=0.001, restart_timeout=0.3) as c:
        started = time.monotonic()
        with pytest.raises(BoardError) as exc:
            c.post(1, "hello")
    assert exc.value.code == "shutting_down" and 0.3 <= time.monotonic() - started < 2


@pytest.mark.parametrize("code, kwargs, tries", [
    ("overloaded", {}, 6),                        # any other 503: max_retries, as before
    ("overloaded", {"max_retries": 0}, 1),
    ("shutting_down", {"max_retries": 0}, 1),     # 0 still means no retries at all
    ("shutting_down", {"restart_timeout": 0}, 6),
])
def test_other_failures_keep_the_retry_count(code, kwargs, tries):
    transport, calls = _answers((503, code))
    with Client("http://board.test", "t", transport=transport, backoff=0.001, **kwargs) as c:
        with pytest.raises(BoardError):
            c.post(1, "hello")
    assert len(calls) == tries


def test_wait_ready_does_not_count_an_answer_after_its_deadline():
    # #28 (review #5 #10): lvwerra's case. A slow but successful health check made wait_ready(1) succeed
    # after 4 s.
    def slow_ok(request):
        time.sleep(1.5)
        return httpx.Response(200, json={"status": "ok"})

    with Client("http://board.test", "", transport=httpx.MockTransport(slow_ok)) as c:
        with pytest.raises(TimeoutError):
            c.wait_ready(0.5)


def test_wait_ready_gives_up_at_its_deadline_on_a_slow_board():
    # #28: each request used the client's full timeout (10 s), so wait_ready(timeout) could overrun by ~20 s.
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(3)
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with Client(f"http://127.0.0.1:{server.server_address[1]}", "") as c:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                c.wait_ready(0.5, interval=0.05)
            assert time.monotonic() - started < 1.5
    finally:
        server.shutdown()
        server.server_close()
