"""
test_zoo_adapter_connection.py — Regression test for zoo adapter fresh connection handling.

Verifies that when a target server silently closes a connection after its first response
without sending a 'Connection: close' header (simulating an idle timeout in a remote container),
subsequent sequential choose_move calls do not attempt to reuse the dead socket and succeed.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
from typing import Any, Dict

from harness import zoo_adapter


class TestZooAdapterConnection(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_url = zoo_adapter.get_target_url()
        self._orig_slug_map = dict(zoo_adapter.SLUG_URL_MAP)

    def tearDown(self) -> None:
        zoo_adapter.set_target_url(self._orig_url)
        zoo_adapter.set_slug_urls(self._orig_slug_map)

    def test_silent_server_disconnect_recovery(self) -> None:
        """
        Server returns HTTP 200 with no Connection: close header, then silently closes the socket.
        Two sequential choose_move calls against it must both succeed and return the server's real moves.
        """
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        port = server_sock.getsockname()[1]
        server_sock.listen(5)

        def run_server() -> None:
            # Request 1: Respond 200 with move "up", then silently close socket (no Connection: close)
            conn1, _ = server_sock.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn1.recv(1024)
                if not chunk:
                    break
                data += chunk
            body1 = json.dumps({"move": "up"}).encode("utf-8")
            resp1 = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body1)).encode("ascii") + b"\r\n"
                b"\r\n" + body1
            )
            conn1.sendall(resp1)
            conn1.close()  # Silent close simulates zoo server idle timeout

            # Request 2: Respond 200 with move "down" on new connection
            conn2, _ = server_sock.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn2.recv(1024)
                if not chunk:
                    break
                data += chunk
            body2 = json.dumps({"move": "down"}).encode("utf-8")
            resp2 = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body2)).encode("ascii") + b"\r\n"
                b"\r\n" + body2
            )
            conn2.sendall(resp2)
            conn2.close()

        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()

        try:
            target_url = f"http://127.0.0.1:{port}"
            zoo_adapter.set_target_url(target_url)
            zoo_adapter.set_slug_urls({})

            game_state: Dict[str, Any] = {
                "game": {"id": "test_game"},
                "board": {"width": 11, "height": 11, "snakes": [], "food": [], "hazards": []},
                "you": {"id": "test_snake", "name": "test_snake"},
            }

            # First move against open server
            m1 = zoo_adapter.choose_move(game_state)
            self.assertEqual(m1, "up")

            # Second move against server after it silently closed the prior socket
            m2 = zoo_adapter.choose_move(game_state)
            self.assertEqual(m2, "down")

            server_thread.join(timeout=2.0)
        finally:
            server_sock.close()


if __name__ == "__main__":
    unittest.main()
