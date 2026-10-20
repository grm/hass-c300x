#!/usr/bin/env python3
"""Watches for a doorbell ring: OpenWebNet frames (monitor session) + incoming SIP requests.

On the first event that looks like a call, waits a few seconds then runs
probe_call.py to talk to the door station.

    python3 tools/watch_ring.py <PswOpen> [--talk-file voice.wav] [--delay 5] [--minutes 30]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "doorentry"))
from sip_client_pure import SipClient  # noqa: E402
from own_client import MON_SESSION, OwnConnection  # noqa: E402

# Door lock / staircase light frames, already known: not a ring
KNOWN = re.compile(r"^\*8\*(19|20|21|22)\*")
LOG = Path("ring_log.txt")


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


class Watcher:
    def __init__(self, args):
        self.args = args
        self.loop: asyncio.AbstractEventLoop | None = None
        self.triggered = False
        self.calling = False

    def trigger(self, reason: str) -> None:
        if self.triggered or self.calling:
            return
        self.triggered = True
        log(f"🔔 RING detected ({reason}) → calling in {self.args.delay:.0f} s")
        asyncio.run_coroutine_threadsafe(self.call(), self.loop)

    async def call(self) -> None:
        await asyncio.sleep(self.args.delay)
        self.calling = True
        try:
            for attempt in range(1, 5):
                log(f"📞 probe_call, attempt {attempt}")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", str(Path(__file__).parent / "probe_call.py"), "20", "--duration", str(self.args.call_duration),
                    "--talk-file", self.args.talk_file, "--talk-delay", "2",
                    "--record", f"ring_call_{attempt}.mkv",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    if any(k in line for k in ("✓", "✗", "←", "🎤", "packets", "BYE", "SIP/2.0 4", "SIP/2.0 6")):
                        log(f"   | {line}")
                if await proc.wait() == 0:
                    break
                await asyncio.sleep(10)
        finally:
            self.calling = False
            log("📞 Call finished (still watching)")

    def own_thread(self) -> None:
        while True:
            try:
                conn = OwnConnection(self.args.gateway_ip)
                conn.authenticate(self.args.password, MON_SESSION)
                log("✓ OpenWebNet monitor open")
                conn.sock.settimeout(None)
                while True:
                    frame = conn.recv()
                    tag = "" if KNOWN.match(frame) else "  ← NEW"
                    log(f"OWN {frame}{tag}{'  (during our call)' if self.calling else ''}")
                    if not KNOWN.match(frame) and not self.calling:
                        self.loop.call_soon_threadsafe(self.trigger, f"OWN {frame}")
            except Exception as err:  # noqa: BLE001
                log(f"✗ OpenWebNet monitor: {err}, reconnecting in 5 s")
                time.sleep(5)

    async def sip(self, creds: dict) -> None:
        client = SipClient(
            gateway_ip=creds["gateway_ip"], gateway_id=creds["gateway_id"],
            sip_account=creds["sip_account"], sip_password=creds["sip_password"],
            cert_file=creds.get("cert_file"), key_file=creds.get("key_file"), ca_file=creds.get("ca_file"),
        )
        if not await client.register():
            log("✗ SIP REGISTER failed")
            return
        log("✓ SIP account registered, waiting for INVITE")
        next_refresh = time.monotonic() + 240
        while True:
            if time.monotonic() > next_refresh:
                client.cseq += 1
                await client._send(client._build_register())
                next_refresh = time.monotonic() + 240
            try:
                msg = await client._receive(timeout=1)
            except TimeoutError:
                continue
            first = msg.split("\r\n", 1)[0]
            if first.startswith("SIP/2.0"):
                if "REGISTER" not in msg.split("CSeq:", 1)[-1].split("\r\n", 1)[0]:
                    log(f"SIP response: {first}")
                continue
            log(f"SIP request: {first}")
            Path(f"ring_sip_{datetime.now():%H%M%S}_{first.split()[0]}.txt").write_text(msg)
            method = first.split()[0]
            if method == "INVITE":
                await client._send(reply(msg, 180, "Ringing"))
                self.loop.call_soon(self.trigger, "SIP INVITE")
            elif method in ("CANCEL", "BYE", "OPTIONS", "MESSAGE", "NOTIFY", "INFO"):
                await client._send(reply(msg, 200, "OK"))


def reply(msg: str, code: int, reason: str) -> str:
    keep = [l for l in msg.split("\r\n")
            if re.match(r"(Via|v|From|f|To|t|Call-ID|i|CSeq|Record-Route):", l, re.IGNORECASE)]
    return "\r\n".join([f"SIP/2.0 {code} {reason}", *keep, "Content-Length: 0", "", ""])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("password", help="PswOpen")
    parser.add_argument("--talk-file", default="test_voix_x3.wav")
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--call-duration", type=float, default=25)
    parser.add_argument("--minutes", type=float, default=30)
    args = parser.parse_args()
    creds = json.loads(Path("sip_credentials.json").read_text())
    args.gateway_ip = creds["gateway_ip"]

    w = Watcher(args)
    w.loop = asyncio.get_running_loop()
    threading.Thread(target=w.own_thread, daemon=True).start()
    log(f"Watching for {args.minutes:.0f} min")
    try:
        await asyncio.wait_for(w.sip(creds), timeout=args.minutes * 60)
    except asyncio.TimeoutError:
        pass
    log("Watch finished")


if __name__ == "__main__":
    asyncio.run(main())
