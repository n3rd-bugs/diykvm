"""Persistent serial-port manager for the KVM serial console.

A port has ONE owner here. When opened it is drained CONTINUOUSLY by a background task into a ring
buffer (scrollback), independent of whether any client is connected, and bytes are fanned out to any
number of subscribed WebSocket clients. This:

  * never loses RX while no client is attached (the buffer keeps the last N bytes),
  * lets several operators watch one port at once, and
  * avoids the "device reports readiness to read but returned no data (multiple access on port)" error
    you get when two readers open the same tty.

Ports are opened **raw, 8N1, echo off, with DTR asserted**, and with selectable **hardware (RTS/CTS)**
or **software (XON/XOFF)** flow control. The port lifecycle (open / close) is explicit and decoupled
from any single client: a client disconnecting does NOT close the port; it keeps buffering until it is
closed via the API (or the service restarts).
"""
import asyncio
import time

import serial

FLOW_MODES = ("none", "rtscts", "xonxoff")
_DEFAULT_BUF = 256 * 1024          # bytes of scrollback retained per open port


class SerialError(Exception):
    pass


def _truthy(v, default=True):
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


class _Port:
    """One open serial device: owns the pyserial handle, the drain task, the ring buffer and subs."""

    def __init__(self, device, baud, flow, dtr, rts, pool, loop, reconnect=True, bufsize=_DEFAULT_BUF):
        self.device = device
        self.baud = int(baud)
        self.flow = flow if flow in FLOW_MODES else "none"
        self.dtr = bool(dtr)
        self.rts = bool(rts)
        self._reconnect = bool(reconnect)
        self._pool = pool
        self._loop = loop
        self._bufsize = bufsize
        self._buf = bytearray()
        self._subs = set()            # set[asyncio.Queue]; each subscriber's live stream
        self._stop = asyncio.Event()
        self._task = None
        self.error = None
        self.reconnects = 0           # how many times we silently re-opened after a disconnect
        self._reconnecting = False    # currently in the re-open backoff loop (port temporarily down)
        self._reopen_req = False      # external request to re-open (e.g. the USB host re-configured the gadget)
        self.rx_bytes = 0             # total bytes received from the device since this port was opened
        self.last_rx = 0.0            # monotonic time of the most recent non-zero read
        # Serialize the handle-destroying ops (close / re-open swap) against an in-flight write() so we never
        # close or swap the pyserial fd from the loop thread while a pool thread is using it. Concurrent
        # read+write is safe (independent directions), so reads are NOT gated on this lock; read-vs-close is
        # kept apart instead by close() awaiting the drain's exit before it closes the handle.
        self._iolock = asyncio.Lock()
        self.ser = self._make_serial()

    def _make_serial(self):
        # Open raw 8N1. pyserial reconfigures the tty to raw (no canonical mode, no echo) on open, so
        # "raw + echo off" is guaranteed regardless of the tty's at-rest termios.
        ser = serial.Serial()
        ser.port = self.device
        ser.baudrate = self.baud
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.rtscts = (self.flow == "rtscts")          # hardware flow control
        ser.xonxoff = (self.flow == "xonxoff")        # software flow control
        ser.dsrdtr = False                            # we drive DTR ourselves; don't gate I/O on DSR
        ser.timeout = 0.1                             # read poll interval -> continuous drain
        ser.write_timeout = 2                         # don't block forever if the peer flow-controls us
        ser.open()
        try:
            ser.dtr = self.dtr                        # read-write with DTR asserted
        except (OSError, ValueError):
            pass
        try:
            if not ser.rtscts:                        # don't fight hardware flow control for RTS
                ser.rts = self.rts
        except (OSError, ValueError):
            pass
        return ser

    def settings(self):
        return (self.baud, self.flow, self.dtr, self.rts)

    def start(self):
        self._task = self._loop.create_task(self._drain())

    async def _drain(self):
        try:
            while not self._stop.is_set():
                if self._reopen_req:                   # forced re-open: the USB host just (re)configured us.
                    self._reopen_req = False            # close+reopen so the TX/write path binds to the live
                    await self._reopen(reason="host reconfigured")   # host -- needed on the FIRST enumeration
                    continue                            # (an open-before-configure leaves TX unbound)
                try:
                    data = await self._loop.run_in_executor(self._pool, self.ser.read, 4096)
                except Exception as e:                 # target re-enumerated / unplugged / read error
                    self.error = str(e)
                    if not self._reconnect:
                        break                          # caller opted out of auto-reopen
                    await self._reopen()               # silently re-open and keep going (obfuscate churn)
                    continue
                if not data:
                    continue
                # RX activity telemetry, surfaced as rx_bytes/last_rx in status().
                self.rx_bytes += len(data)
                self.last_rx = time.monotonic()
                if self._subs:
                    # Live clients attached: deliver straight to them and DON'T retain a copy, so the same
                    # bytes are never replayed to a later / reconnecting client.
                    for q in list(self._subs):
                        try:
                            q.put_nowait(data)
                        except asyncio.QueueFull:
                            pass      # slow client; drop (it's live data, not buffered)
                else:
                    # Nobody attached: keep a bounded backlog of what arrived while detached.
                    self._buf += data
                    if len(self._buf) > self._bufsize:
                        del self._buf[:len(self._buf) - self._bufsize]
        finally:
            self._stop.set()
            for q in list(self._subs):
                try:
                    q.put_nowait(None)   # sentinel so blocked subscribers wake and exit (port closed)
                except asyncio.QueueFull:
                    pass

    async def _reopen(self, reason="disconnect"):
        """Re-open the device (after a disconnect, or when the USB host re-configured us), retrying with
        backoff until it works or we're stopped. Subscribers stay attached and the buffer is preserved, so
        the re-open is invisible to them -- no per-flap disconnect/reconnect churn on the console."""
        self._reconnecting = True
        async with self._iolock:                       # close the dead handle without racing a write()
            try:
                # Bound the close: on a wedged gadget tty, ser.close() can block indefinitely, which would
                # hold _iolock (and burn a pool worker) forever and freeze every reopen. wait_for stops us
                # awaiting it past the timeout (the close may finish later in its thread; the fd is dead).
                await asyncio.wait_for(self._loop.run_in_executor(self._pool, self.ser.close), timeout=5.0)
            except Exception:
                pass                                   # includes asyncio.TimeoutError (a subclass of Exception)
        delay = 0.3
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return                                 # stopped while backing off -> abort the re-open
            except asyncio.TimeoutError:
                pass                                   # backoff elapsed -> try to re-open
            try:
                async with self._iolock:               # swap the fd atomically vs write()
                    self.ser = await self._loop.run_in_executor(self._pool, self._make_serial)
            except Exception as e:
                self.error = str(e)
                delay = min(delay * 1.5, 3.0)          # back off but keep trying (target may be cycling)
                continue
            self.reconnects += 1
            self.error = None
            self._reconnecting = False
            print("[serial] %s re-opened (%s, #%d)" % (self.device, reason, self.reconnects), flush=True)
            return

    @property
    def alive(self):
        return self._task is not None and not self._task.done()

    def subscribe(self):
        # Hand over the backlog accumulated while detached, then DROP it: once delivered over a socket it
        # must not be replayed (no duplication on reconnect). Returns (queue, backlog_bytes).
        q = asyncio.Queue(maxsize=2048)
        backlog = bytes(self._buf)
        self._buf.clear()
        self._subs.add(q)
        return q, backlog

    def unsubscribe(self, q):
        self._subs.discard(q)

    def clear_buffer(self):
        """Drop the detached scrollback ring buffer -- the bytes that arrived while no client was attached
        and would be replayed to the next subscriber (the `buffered` count in status()). Returns the number
        of bytes dropped. Live subscribers' in-flight data and the lifetime rx_bytes counter are untouched.
        Event-loop only: `_buf` is mutated by the drain on the loop, so this stays race-free without a lock."""
        n = len(self._buf)
        self._buf.clear()
        return n

    def request_reopen(self):
        """Ask the drain to close+reopen the port at its next loop turn (within ~the read timeout). Used when
        the USB host re-configures the gadget, so the TX/write path re-binds to the live host. Idempotent and
        cheap; keeps subscribers + buffer."""
        self._reopen_req = True

    async def write(self, data):
        async with self._iolock:                       # never write while close()/_reopen() is swapping the fd
            await self._loop.run_in_executor(self._pool, self.ser.write, data)

    async def close(self):
        self._stop.set()
        t = self._task
        if t:
            # Let the drain exit on its own (it checks _stop after each <=0.1s read, and the _reopen backoff
            # wakes on _stop) instead of cancelling -- so we never tear down the handle while a read is still
            # in flight on a pool thread. Cancel only as a last resort if it somehow wedges.
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()
                try:
                    await t
                except Exception:
                    pass
            except Exception:
                pass
        # Bound the handle close: a wedged gadget-tty read (host gone / gadget mid-rebuild) can make
        # ser.close() block, which would otherwise hang shutdown until systemd SIGKILLs the service. If it
        # doesn't finish promptly, abandon the worker thread (we're tearing the port down anyway).
        try:
            await asyncio.wait_for(self._close_handle(), timeout=3.0)
        except Exception:
            pass
        if t is None:                                  # task never started: the drain's finally never ran
            for q in list(self._subs):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _close_handle(self):
        async with self._iolock:                       # serialize the final close vs any in-flight write()
            await self._loop.run_in_executor(self._pool, self.ser.close)


class SerialManager:
    """Registry of open ports, keyed by device path."""

    def __init__(self, pool):
        self._pool = pool
        self._ports = {}              # device -> _Port
        self._locks = {}              # device -> asyncio.Lock serializing open/close per device

    def _lock_for(self, device):
        # Per-device lock: open() awaits mid-sequence (await p.close()), and a concurrent open/close for
        # the SAME tty in that window could register a port the first coroutine then pops -- an
        # unreachable orphan whose drain task keeps the tty open and steals RX forever.
        if device not in self._locks:
            self._locks[device] = asyncio.Lock()
        return self._locks[device]

    async def open(self, device, baud, flow="none", dtr=True, rts=True, reconnect=True, reopen=False):
        """Open the port (idempotent). Reopens if settings differ or reopen=True. reconnect=True makes the
        port self-heal: it silently re-opens on disconnect (target re-enumeration) without dropping clients."""
        loop = asyncio.get_event_loop()
        flow = flow if flow in FLOW_MODES else "none"
        baud = int(baud)
        async with self._lock_for(device):
            p = self._ports.get(device)
            if p and p.alive:
                if not reopen and p.settings() == (baud, flow, bool(dtr), bool(rts)):
                    p._reconnect = bool(reconnect)   # update the behavior flag on reuse
                    return p              # already open with the same settings; reuse
                await p.close()
                self._ports.pop(device, None)
            try:
                p = _Port(device, baud, flow, dtr, rts, self._pool, loop, reconnect=reconnect)
            except Exception as e:
                raise SerialError(str(e))
            p.start()
            self._ports[device] = p
            return p

    def get(self, device):
        p = self._ports.get(device)
        return p if (p and p.alive) else None

    async def close(self, device):
        async with self._lock_for(device):
            p = self._ports.pop(device, None)
            if not p:
                return False
            await p.close()
            return True

    def clear(self, device):
        """Clear an open port's scrollback buffer. Returns the number of bytes dropped, or None if the
        device is not currently open. Call from the event loop (the API handler) so it stays serialized
        with the drain that fills the buffer."""
        p = self.get(device)
        if p is None:
            return None
        return p.clear_buffer()

    def status(self):
        out = []
        for dev, p in list(self._ports.items()):
            out.append({
                "device": dev, "baud": p.baud, "flow": p.flow, "dtr": p.dtr, "rts": p.rts,
                "open": p.alive, "buffered": len(p._buf), "clients": len(p._subs),
                "reconnect": p._reconnect, "reconnects": p.reconnects, "reconnecting": p._reconnecting,
                "rx_bytes": p.rx_bytes, "last_rx": round(p.last_rx, 3), "error": p.error,
            })
        return out

    def reopen_gadget_ports(self):
        """Force the Pi's own gadget COM port(s) (/dev/ttyGS*) to close+reopen, so their TX/write path binds
        to a freshly-(re)configured USB host. Called on the target's USB 'configured' transition.
        USB/RS-232 adapters (ttyUSB*/ttyACM*) are unaffected."""
        done = []
        for dev, p in list(self._ports.items()):
            if dev.startswith("/dev/ttyGS") and p.alive and not p._reconnecting:
                p.request_reopen()
                done.append(dev)
        return done

    async def close_all(self):
        for dev in list(self._ports):
            await self.close(dev)
