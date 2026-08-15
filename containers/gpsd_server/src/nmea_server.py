"""Serve synthesized NMEA on loopback for gpsd to consume.

gpsd is pointed at ``tcp://127.0.0.1:<nmea_port>`` and treats this like any
other network GPS source. A loopback socket is used rather than a FIFO or a
pty pretending to be a serial device: daemons commonly reject anything that
fails their device probe, and FIFO open semantics create startup-order
deadlocks. Either side can also restart independently this way.

The listener binds to 127.0.0.1 only. This is an internal seam, not a service,
so it must never be reachable from the network.
"""

import socket
import threading
import time
from typing import Callable, List, Optional

import cp

from models import Fix


class NmeaServer:
    """Push NMEA sentences to every connected loopback client."""

    def __init__(
        self,
        stop_event: threading.Event,
        port: int,
        fix_provider: Callable[[], Fix],
        sentence_builder: Callable[[Fix], List[str]],
        emit_interval: float = 1.0,
    ) -> None:
        self._stop = stop_event
        self._port = port
        self._fix_provider = fix_provider
        self._sentence_builder = sentence_builder
        self._emit_interval = emit_interval
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._client_count = 0
        self._sentences_sent = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "port": self._port,
                "clients": self._client_count,
                "sentences_sent": self._sentences_sent,
                "listening": self._listener is not None,
            }

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self._port))
        listener.listen(8)
        # Bounded accept so the loop can notice the stop event.
        listener.settimeout(1.0)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="nmea-listener", daemon=True)
        self._thread.start()
        cp.log(f"nmea: listening on 127.0.0.1:{self._port}")

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def close(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    cp.log("nmea: listener socket closed unexpectedly")
                break
            threading.Thread(
                target=self._serve_client,
                args=(client, address),
                name="nmea-client",
                daemon=True,
            ).start()
        self.close()
        cp.log("nmea: listener stopped")

    def _serve_client(self, client: socket.socket, address) -> None:
        with self._lock:
            self._client_count += 1
        cp.log(f"nmea: client connected from {address[0]}:{address[1]}")
        try:
            client.settimeout(5.0)
            while not self._stop.is_set():
                started = time.time()
                fix = self._fix_provider()
                payload = "".join(self._sentence_builder(fix)).encode("ascii", "ignore")
                try:
                    client.sendall(payload)
                except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                    break
                with self._lock:
                    self._sentences_sent += 1
                self._stop.wait(max(0.05, self._emit_interval - (time.time() - started)))
        finally:
            try:
                client.close()
            except OSError:
                pass
            with self._lock:
                self._client_count -= 1
            cp.log(f"nmea: client {address[0]}:{address[1]} disconnected")
