"""Live BTC/USD price feed from Polymarket's own Chainlink oracle (RTDS).

This is the exact price source Polymarket uses to settle the 5-minute BTC
up/down markets, so using it means our signal and our resolution match the
market perfectly. Public websocket, no API key.

The feed runs in a background thread, auto-reconnects, and keeps:
  - latest: the most recent (timestamp_sec, price)
  - history: {second_timestamp: price} for the last ~15 minutes, so we can
    look up the price at any window boundary (price-to-beat / settlement).
"""
import json
import threading
import time

import websocket  # websocket-client

import config


class ChainlinkFeed:
    def __init__(self):
        self.latest_ts = None
        self.latest_price = None
        self.history = {}          # sec_ts -> price
        self._lock = threading.Lock()
        self._ws = None
        self._stop = False

    # ---- public API ----
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def wait_ready(self, timeout=20) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.latest_price is not None:
                return True
            time.sleep(0.2)
        return False

    def price_now(self):
        with self._lock:
            return self.latest_price

    def price_at(self, sec_ts: int):
        """Price at/just after a boundary second, like Polymarket: first tick
        with timestamp >= sec_ts. Searches a few seconds forward."""
        with self._lock:
            for t in range(sec_ts, sec_ts + 10):
                if t in self.history:
                    return self.history[t]
        return None

    def recent_prices(self, lookback_sec: int):
        now = int(time.time())
        with self._lock:
            return [self.history[t] for t in range(now - lookback_sec, now + 1)
                    if t in self.history]

    # ---- internals ----
    def _store(self, ts_ms, value):
        sec = int(ts_ms // 1000)
        with self._lock:
            self.latest_ts = sec
            self.latest_price = float(value)
            self.history[sec] = float(value)
            # prune to ~15 min
            cutoff = sec - 900
            for k in [k for k in self.history if k < cutoff]:
                del self.history[k]

    def _on_open(self, ws):
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": config.CHAINLINK_TOPIC,
                "type": "*",
                "filters": json.dumps({"symbol": config.CHAINLINK_SYMBOL}),
            }],
        }))

    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
        except Exception:
            return
        payload = d.get("payload", {})
        # live single update
        if "value" in payload and "timestamp" in payload:
            self._store(payload["timestamp"], payload["value"])
        # initial bulk history
        data = payload.get("data")
        if isinstance(data, list):
            for row in data:
                if "value" in row and "timestamp" in row:
                    self._store(row["timestamp"], row["value"])

    def _run(self):
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    config.RTDS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print("  feed error:", e)
            if not self._stop:
                time.sleep(3)  # reconnect backoff

    def stop(self):
        self._stop = True
        if self._ws:
            self._ws.close()
