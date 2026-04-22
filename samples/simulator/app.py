#!/usr/bin/env python3
"""
OOM simulator — runs in one of several modes to reproduce memory-failure
patterns on Kubernetes so you can validate the Thanos OOM dashboard.

MODES (MODE env var):
  leak      Allocates LEAK_MB_PER_SEC MiB every second, forever.
            -> Pattern A: steady leak, deriv positive.
  spike     HTTP server. GET /spike allocates SPIKE_MB briefly.
            GET /leak permanently appends SPIKE_MB. GET /free clears.
            -> Pattern B with loadgen.
  baseline  Allocates BASELINE_MB once, then idles.
            -> Pattern C: high baseline near limit.
  startup   Allocates STARTUP_MB immediately at boot, before HTTP ready.
            -> Pattern E: dies in <60s if STARTUP_MB > container limit.
  idle      Allocates nothing. Useful as a control.

ENV VARS:
  MODE              leak | spike | baseline | startup | idle   (default: leak)
  LEAK_MB_PER_SEC   float, MiB added per second in leak mode    (default: 5)
  BASELINE_MB       int, MiB held in baseline mode              (default: 100)
  STARTUP_MB        int, MiB allocated at startup               (default: 500)
  SPIKE_MB          int, MiB per /spike or /leak request        (default: 50)
  PORT              HTTP port for spike mode                    (default: 8080)
"""
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

MODE            = os.getenv("MODE", "leak").strip().lower()
LEAK_MB_PER_SEC = float(os.getenv("LEAK_MB_PER_SEC", "5"))
BASELINE_MB     = int(os.getenv("BASELINE_MB", "100"))
STARTUP_MB      = int(os.getenv("STARTUP_MB", "500"))
SPIKE_MB        = int(os.getenv("SPIKE_MB", "50"))
PORT            = int(os.getenv("PORT", "8080"))

MB = 1024 * 1024
_hold = []  # holds references so memory stays allocated
_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[oom-simulator] {msg}", flush=True)


def allocate_mb(n: int) -> None:
    """bytearray(n) allocates AND zero-fills, so RSS actually grows."""
    with _lock:
        _hold.append(bytearray(n * MB))


def total_mb() -> int:
    with _lock:
        return sum(len(b) for b in _hold) // MB


# -------------------- MODES --------------------

def leak_mode() -> None:
    log(f"mode=leak  rate={LEAK_MB_PER_SEC} MiB/s")
    chunk = int(LEAK_MB_PER_SEC * MB)
    while True:
        with _lock:
            _hold.append(bytearray(chunk))
        if int(time.time()) % 10 == 0:
            log(f"holding ~{total_mb()} MiB")
        time.sleep(1.0)


def baseline_mode() -> None:
    log(f"mode=baseline  allocating {BASELINE_MB} MiB, then idle")
    allocate_mb(BASELINE_MB)
    log(f"holding {total_mb()} MiB")
    while True:
        time.sleep(3600)


def startup_mode() -> None:
    log(f"mode=startup  allocating {STARTUP_MB} MiB at boot")
    allocate_mb(STARTUP_MB)
    log(f"holding {total_mb()} MiB  (if >limit, OOM fires now)")
    while True:
        time.sleep(3600)


class SpikeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log("HTTP " + fmt % args)

    def _reply(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/spike":
            block = bytearray(SPIKE_MB * MB)    # transient spike
            time.sleep(10)
            del block
            self._reply(200, f"spike {SPIKE_MB} MiB done\n")
        elif path == "/leak":
            with _lock:
                _hold.append(bytearray(SPIKE_MB * MB))
            self._reply(200, f"leaked {SPIKE_MB} MiB, holding {total_mb()} MiB\n")
        elif path == "/free":
            with _lock:
                _hold.clear()
            self._reply(200, "freed all\n")
        elif path == "/stats":
            self._reply(200, f"mode={MODE} holding={total_mb()} MiB\n")
        elif path == "/healthz":
            self._reply(200, "ok\n")
        else:
            self._reply(200, "endpoints: /spike /leak /free /stats /healthz\n")


def spike_mode() -> None:
    log(f"mode=spike  listening on :{PORT} spike_size={SPIKE_MB} MiB")
    HTTPServer(("0.0.0.0", PORT), SpikeHandler).serve_forever()


def idle_mode() -> None:
    log("mode=idle  no allocation, control pod")
    while True:
        time.sleep(3600)


MODES = {
    "leak":     leak_mode,
    "spike":    spike_mode,
    "baseline": baseline_mode,
    "startup":  startup_mode,
    "idle":     idle_mode,
}


def main() -> None:
    if MODE not in MODES:
        log(f"unknown MODE={MODE}  valid: {', '.join(MODES)}")
        sys.exit(1)
    try:
        MODES[MODE]()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
