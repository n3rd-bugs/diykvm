"""In-process event bus for the KVM control plane.

A background poller and the request handlers publish state-change events here; WebSocket clients
(/ws/events) subscribe and receive them as JSON, so external tooling can track USB-gadget, serial,
virtual-drive, power and switch state in real time without polling each API. Recent events are kept in a
small ring buffer so a connecting client can optionally replay what it just missed.

Each event is a flat dict: {"seq": <monotonic int>, "ts": <unix seconds>, "type": <str>, ...fields}.
"""
import asyncio
import time


class EventBus:
    def __init__(self, ring: int = 256):
        self._subs = set()          # set[asyncio.Queue]
        self._ring = []             # recent events, for replay-on-connect
        self._ring_max = ring
        self._seq = 0
        self._loop = None           # bound at startup so publish() is safe from worker threads

    def bind(self, loop):
        """Record the event loop so publish() can be called from worker threads (sync routes). asyncio.Queue
        and the loop's Futures are NOT thread-safe, so off-loop publishes are marshaled onto the loop."""
        self._loop = loop

    def publish(self, type: str, **fields):
        loop = self._loop
        if loop is not None:
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if not on_loop:                      # called from a worker thread -> hop onto the loop
                loop.call_soon_threadsafe(lambda: self._publish(type, fields))
                return None
        return self._publish(type, fields)

    def _publish(self, type: str, fields: dict) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "ts": round(time.time(), 3), "type": type}
        ev.update(fields)
        self._ring.append(ev)
        if len(self._ring) > self._ring_max:
            del self._ring[:len(self._ring) - self._ring_max]
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass                # slow client; it can re-sync via the snapshot / /api/events/state
        return ev

    def subscribe(self, replay: int = 0):
        """Register a subscriber. Returns (queue, recent_events) where recent_events is up to `replay` of
        the most recent events (oldest first), so a client can see what happened just before it connected."""
        q = asyncio.Queue(maxsize=1024)
        recent = list(self._ring[-replay:]) if replay > 0 else []
        self._subs.add(q)
        return q, recent

    def unsubscribe(self, q):
        self._subs.discard(q)

    @property
    def seq(self) -> int:
        return self._seq
