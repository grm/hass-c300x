#!/usr/bin/env python3
"""C300X video call probe: sends an INVITE to c300x with DEVADDR, prints the SDP answer,
decrypts SRTP, relays audio and video to ffplay/ffmpeg, can send the microphone, then hangs up.

Usage:
    python3 tools/probe_call.py [devaddr] [--duration 15] [--ffplay | --record out.mkv|snap.jpg]
                                [--talk | --talk-file voice.wav]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "doorentry"))
from sip_client_pure import SipClient, _status_code  # noqa: E402
from srtp import SrtpContext, new_inline_key  # noqa: E402

AUDIO_PORT = 40000
VIDEO_PORT = 40002
AUDIO_RELAY_PORT = 50000
VIDEO_RELAY_PORT = 50002
SPEEX_FRAME_BYTES = 320  # 160 samples s16le = 20 ms at 8 kHz

VFU_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?><media_control><vc_primitive><to_encoder>'
    "<picture_fast_update></picture_fast_update></to_encoder></vc_primitive></media_control>"
)

_LOGGER = logging.getLogger("probe")


def build_offer(local_ip: str, devaddr: str, srtp: bool, audio_key: str, video_key: str,
                video_extra: list[str] = (), audio_pts: str = "96 97 98 0 8 101") -> str:
    proto = "RTP/SAVP" if srtp else "RTP/AVP"
    sess = int(time.time())
    lines = [
        "v=0",
        f"o=- {sess} {sess} IN IP4 {local_ip}",
        "s=Talk",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"a=DEVADDR:{devaddr}",
        f"m=audio {AUDIO_PORT} {proto} {audio_pts}",
        "a=rtpmap:96 opus/48000/2",
        "a=fmtp:96 useinbandfec=1",
        "a=rtpmap:97 speex/16000",
        "a=rtpmap:98 speex/8000",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=sendrecv",
    ]
    if srtp:
        lines.append(f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{audio_key}")
    lines += [
        f"m=video {VIDEO_PORT} {proto} 99 103",
        *[x for x in video_extra if x.startswith("b=")],
        "a=rtpmap:99 H264/90000",
        "a=fmtp:99 profile-level-id=42801F;packetization-mode=1",
        "a=rtpmap:103 VP8/90000",
        *[x for x in video_extra if not x.startswith("b=")],
        "a=recvonly",
    ]
    if srtp:
        lines.append(f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{video_key}")
    return "\r\n".join(lines) + "\r\n"


def header(msg: str, name: str) -> str | None:
    m = re.search(rf"^{name}:\s*(.+?)\r?$", msg, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else None


def headers(msg: str, name: str) -> list[str]:
    return [h.strip() for h in re.findall(rf"^{name}:\s*(.+?)\r?$", msg, re.IGNORECASE | re.MULTILINE)]


def body_of(msg: str) -> str:
    return msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""


class RtpCounter(asyncio.DatagramProtocol):
    def __init__(self, name: str):
        self.name = name
        self.packets = 0
        self.bytes = 0
        self.auth_failures = 0
        self.payload_types: dict[int, int] = {}
        self.remote = None
        self.srtp: SrtpContext | None = None
        # Packets are buffered until the player is ready so the first keyframe isn't lost
        self.relay_to: tuple[str, int] | None = None
        self.relay_buffer: list[bytes] | None = None

    def connection_made(self, transport):
        self.transport = transport

    def start_buffering(self, target: tuple[str, int]) -> None:
        self.relay_to = target
        self.relay_buffer = []

    def flush(self) -> None:
        if self.relay_buffer is None:
            return
        for pkt in self.relay_buffer:
            self.transport.sendto(pkt, self.relay_to)
        print(f"  → {len(self.relay_buffer)} buffered {self.name} packets relayed to the player")
        self.relay_buffer = None

    def datagram_received(self, data, addr):
        self.remote = addr
        self.packets += 1
        self.bytes += len(data)
        if self.packets == 1:
            print(f"  ✓ First {self.name} packet received from {addr[0]}:{addr[1]}")
        if len(data) < 12 or data[0] >> 6 != 2:
            return  # STUN or garbage
        pt = data[1] & 0x7F
        self.payload_types[pt] = self.payload_types.get(pt, 0) + 1
        if self.srtp:
            data = self.srtp.unprotect(data)
            if data is None:
                self.auth_failures += 1
                return
        if self.relay_to:
            if self.relay_buffer is not None:
                self.relay_buffer.append(data)
            else:
                self.transport.sendto(data, self.relay_to)


class Probe:
    def __init__(self, client: SipClient, local_ip: str, local_port: int):
        self.c = client
        self.local_ip = local_ip
        self.local_port = local_port
        self.call_id = str(uuid.uuid4())
        self.from_tag = client._generate_tag()
        self.cseq = 1
        self.target = f"sip:c300x@{client.domain}"
        self.to_hdr = f"<{self.target}>"
        self.remote_uri = self.target
        self.route_set: list[str] = []
        self.branch = ""

    def contact(self) -> str:
        return f"<sip:{self.c.username}@{self.local_ip}:{self.local_port};transport=tls>"

    def request(self, method: str, uri: str, cseq: int, branch: str, extra: list[str], body: str = "") -> str:
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.c.sip_account}>;tag={self.from_tag}",
            f"To: {self.to_hdr}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            *[f"Route: {r}" for r in self.route_set],
            *extra,
            "User-Agent: HomeAssistant-DoorEntry/0.1.0",
            f"Content-Length: {len(body.encode())}",
            "",
            body,
        ]
        return "\r\n".join(lines)

    def invite(self, sdp: str, auth: str | None = None) -> str:
        self.branch = self.c._generate_branch()
        extra = [
            f"Contact: {self.contact()}",
            "Allow: INVITE, ACK, CANCEL, OPTIONS, BYE, REFER, NOTIFY, MESSAGE, SUBSCRIBE, INFO, UPDATE",
            "Content-Type: application/sdp",
        ]
        if auth:
            extra.append(auth)
        return self.request("INVITE", self.target, self.cseq, self.branch, extra, sdp)

    def ack_non2xx(self, resp: str) -> str:
        to = header(resp, "To") or self.to_hdr
        lines = [
            f"ACK {self.target} SIP/2.0",
            f"Via: SIP/2.0/TLS {self.local_ip}:{self.local_port};branch={self.branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.c.sip_account}>;tag={self.from_tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} ACK",
            "Content-Length: 0",
            "",
            "",
        ]
        return "\r\n".join(lines)

    def auth_header(self, resp: str) -> str | None:
        code = _status_code(resp)
        challenge = header(resp, "WWW-Authenticate" if code == 401 else "Proxy-Authenticate")
        if not challenge:
            return None
        realm = re.search(r'realm="([^"]+)"', challenge)
        nonce = re.search(r'nonce="([^"]+)"', challenge)
        if not realm or not nonce:
            return None
        digest = self.c._calculate_digest_response("INVITE", self.target, nonce.group(1), realm.group(1))
        name = "Authorization" if code == 401 else "Proxy-Authorization"
        return (
            f'{name}: Digest username="{self.c.username}", realm="{realm.group(1)}", '
            f'nonce="{nonce.group(1)}", uri="{self.target}", response="{digest}", algorithm=MD5'
        )

    def establish_dialog(self, resp: str) -> None:
        self.to_hdr = header(resp, "To") or self.to_hdr
        contact = header(resp, "Contact")
        if contact:
            m = re.search(r"<([^>]+)>", contact)
            self.remote_uri = m.group(1) if m else contact.split(";")[0]
        rr = []
        for h in headers(resp, "Record-Route"):
            rr += [x.strip() for x in h.split(",")]
        self.route_set = list(reversed(rr))

    def reply_200(self, req: str) -> str:
        lines = ["SIP/2.0 200 OK"]
        for name in ("Via", "From", "To", "Call-ID", "CSeq"):
            lines += [f"{name}: {v}" for v in headers(req, name)]
        lines += ["Content-Length: 0", "", ""]
        return "\r\n".join(lines)


def media_sections(sdp: str) -> dict[str, dict]:
    """Per media: port, payload types, raw section text, inline SRTP key."""
    out = {}
    parts = re.split(r"(?m)^(?=m=)", sdp)
    for part in parts[1:]:
        m = re.match(r"m=(\w+) (\d+) (\S+) ([\d ]+)", part)
        if not m:
            continue
        crypto = re.search(r"^a=crypto:\d+ \S+ inline:(\S+)", part, re.MULTILINE)
        out[m.group(1)] = {
            "port": int(m.group(2)),
            "pts": m.group(4).split(),
            "text": part,
            "key": crypto.group(1) if crypto else None,
        }
    return out


def write_player_sdp(media: dict[str, dict], path: Path, with_audio: bool) -> bool:
    lines = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=C300X", "c=IN IP4 127.0.0.1", "t=0 0"]
    found = False
    for kind, port in (("video", VIDEO_RELAY_PORT), ("audio", AUDIO_RELAY_PORT)):
        sec = media.get(kind)
        if not sec or sec["port"] == 0 or (kind == "audio" and not with_audio):
            continue
        pt = sec["pts"][0]
        lines.append(f"m={kind} {port} RTP/AVP {pt}")
        for attr in ("rtpmap", "fmtp"):
            a = re.search(rf"^a={attr}:{pt} (.+?)\r?$", sec["text"], re.MULTILINE)
            if a:
                lines.append(f"a={attr}:{pt} {a.group(1)}")
        found = True
    path.write_text("\n".join(lines) + "\n")
    return found


def player_command(args) -> list[str] | None:
    common = ["-protocol_whitelist", "file,udp,rtp", "-i", "video.sdp"]
    if args.ffplay:
        exe = shutil.which("ffplay")
        return exe and [exe, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer",
                        "-window_title", "C300X", *common]
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    base = [exe, "-hide_banner", "-nostats", "-loglevel", "warning", "-y", *common]
    if args.record.lower().endswith((".jpg", ".png")):
        return [*base, "-map", "0:v", "-frames:v", "1", "-update", "1", args.record]
    if args.record.lower().endswith(".wav"):
        return [*base, "-map", "0:a", args.record]
    return [*base, "-c:v", "copy", "-c:a", "aac", "-ar", "16000", "-ac", "1", args.record]


def stop_player(proc: subprocess.Popen, args) -> None:
    # SIGINT (not SIGTERM) lets ffmpeg write the container trailer
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if args.record and Path(args.record).exists():
        print(f"✓ Recorded: {args.record} ({Path(args.record).stat().st_size} bytes)")


def talk_filter(gain_db: float) -> str:
    """Telephone band + compression: louder speech without clipping on the door station speaker."""
    makeup = min(64.0, max(1.0, 10 ** (gain_db / 20)))  # acompressor takes a linear factor
    return ("highpass=f=200,lowpass=f=3400,"
            f"acompressor=threshold=-24dB:ratio=6:attack=5:release=150:makeup={makeup:.2f},"
            "alimiter=limit=0.8:level=0")  # headroom: Speex overshoots peaks on decode


async def talk(args, audio_sock, dst: tuple[str, int], pt: int, key: str, stop: asyncio.Event) -> int:
    """Capture mic (or a file), encode Speex, protect with our SRTP key, send to the gateway."""
    from speex import SpeexEncoder

    try:
        await asyncio.wait_for(stop.wait(), timeout=args.talk_delay)
        return 0
    except asyncio.TimeoutError:
        pass
    encoder = SpeexEncoder(quality=args.speex_quality)
    if args.talk_file:
        source = ["-i", args.talk_file]
    else:
        source = ["-f", "avfoundation", "-i", f":{args.mic}"]
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *source,
        "-af", talk_filter(args.talk_gain), "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
        stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    ctx = SrtpContext(key)
    ssrc = int.from_bytes(os.urandom(4), "big")
    seq = int.from_bytes(os.urandom(2), "big")
    ts = int.from_bytes(os.urandom(4), "big")
    sent = 0
    started = time.monotonic()
    print(f"  🎤 Sending audio to {dst[0]}:{dst[1]} ({'file ' + args.talk_file if args.talk_file else 'microphone'})")
    try:
        while not stop.is_set():
            try:
                pcm = await proc.stdout.readexactly(SPEEX_FRAME_BYTES)
            except asyncio.IncompleteReadError:
                break
            marker = 0x80 if sent == 0 else 0
            header = struct.pack("!BBHII", 0x80, marker | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
            # Pace on our own clock: bursts overflow the door station jitter buffer (clicks)
            delay = started + sent * 0.02 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            audio_sock.sendto(ctx.protect(header + encoder.encode(pcm)), dst)
            seq += 1
            ts += 160
            sent += 1
        print(f"  🎤 Audio sending finished ({time.monotonic() - started:.1f} s)")
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()
        encoder.close()
    return sent


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("devaddr", nargs="?", default="20", help="Door station address (dev+addr), default 20")
    parser.add_argument("--duration", type=float, default=15, help="Call duration in seconds")
    parser.add_argument("--no-srtp", dest="srtp", action="store_false", help="Offer unencrypted RTP/AVP (rejected by the gateway)")
    parser.add_argument("--ffplay", action="store_true", help="Display the video with ffplay")
    parser.add_argument("--video-bw", type=int, metavar="KBPS", help="Requested video bitrate (b=AS)")
    parser.add_argument("--imageattr", metavar="WxH", help="Requested resolution, e.g. 1280x720")
    parser.add_argument("--h264-fmtp", metavar="FMTP", help="Replace the offered H264 fmtp")
    parser.add_argument("--audio-pts", default="96 97 98 0 8 101", metavar="PTS",
                        help="Offered audio payload types, in order of preference")
    parser.add_argument("--record", metavar="FILE",
                        help="Record audio+video (.mkv/.mp4), audio only (.wav) or a still image (.jpg)")
    parser.add_argument("--no-listen", dest="listen", action="store_false", help="Do not play the received audio")
    parser.add_argument("--talk-delay", type=float, default=0, metavar="S",
                        help="Wait S seconds after the call is established before talking")
    parser.add_argument("--talk", action="store_true", help="Send the Mac's microphone to the door station")
    parser.add_argument("--talk-file", metavar="FILE", help="Send an audio file to the door station")
    parser.add_argument("--talk-gain", type=float, default=18, metavar="DB",
                        help="Make-up gain after compression of the sent voice, 0 to 36 dB (default 18)")
    parser.add_argument("--speex-quality", type=int, default=10, choices=range(11), metavar="0-10",
                        help="Speex encoder quality (default 10)")
    parser.add_argument("--mic", default="0", help="avfoundation microphone index (default 0)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print SIP messages")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    creds = json.loads(Path("sip_credentials.json").read_text())
    client = SipClient(
        gateway_ip=creds["gateway_ip"],
        gateway_id=creds["gateway_id"],
        sip_account=creds["sip_account"],
        sip_password=creds["sip_password"],
        cert_file=creds.get("cert_file"),
        key_file=creds.get("key_file"),
        ca_file=creds.get("ca_file"),
    )

    print("→ REGISTER...")
    if not await client.register():
        print("✗ REGISTER failed")
        await client.close()
        return 1
    print("✓ Registered")

    local_ip, local_port = client._writer.get_extra_info("sockname")[:2]
    print(f"→ Local IP: {local_ip}")

    loop = asyncio.get_running_loop()
    audio = RtpCounter("audio")
    video = RtpCounter("video")
    transports = []
    for port, proto in ((AUDIO_PORT, audio), (AUDIO_PORT + 1, RtpCounter("rtcp-audio")),
                        (VIDEO_PORT, video), (VIDEO_PORT + 1, RtpCounter("rtcp-video"))):
        t, _ = await loop.create_datagram_endpoint(lambda p=proto: p, local_addr=("0.0.0.0", port))
        transports.append(t)
    use_player = args.ffplay or args.record
    if use_player:
        video.start_buffering(("127.0.0.1", VIDEO_RELAY_PORT))
        if args.listen:
            audio.start_buffering(("127.0.0.1", AUDIO_RELAY_PORT))
    audio_key, video_key = new_inline_key(), new_inline_key()

    probe = Probe(client, local_ip, local_port)
    video_extra = []
    if args.video_bw:
        video_extra.append(f"b=AS:{args.video_bw}")
    if args.imageattr:
        w, h = args.imageattr.lower().split("x")
        video_extra += [f"a=imageattr:99 recv [x={w},y={h}]", f"a=imageattr:103 recv [x={w},y={h}]"]
    offer = build_offer(local_ip, args.devaddr, args.srtp, audio_key, video_key, video_extra, args.audio_pts)
    if args.h264_fmtp:
        offer = offer.replace("a=fmtp:99 profile-level-id=42801F;packetization-mode=1", f"a=fmtp:99 {args.h264_fmtp}")
    print(f"\n{'='*60}\nSDP OFFER\n{'='*60}\n{offer}")

    await client._send(probe.invite(offer))
    answer_sdp = None
    established = False
    authed = False
    deadline = time.monotonic() + 20

    while time.monotonic() < deadline:
        try:
            msg = await client._receive(timeout=deadline - time.monotonic())
        except TimeoutError:
            break
        first = msg.split("\r\n", 1)[0]
        if not first.startswith("SIP/2.0"):
            print(f"← Request received: {first}")
            await client._send(probe.reply_200(msg))
            continue
        if "INVITE" not in (header(msg, "CSeq") or ""):
            continue
        code = _status_code(msg)
        print(f"← {first}")
        if body_of(msg).strip() and answer_sdp is None and code in (183, 200):
            answer_sdp = body_of(msg)
        if code < 200:
            continue
        if code in (401, 407) and not authed:
            await client._send(probe.ack_non2xx(msg))
            auth = probe.auth_header(msg)
            if not auth:
                print("✗ Challenge without realm/nonce")
                break
            authed = True
            probe.cseq += 1
            await client._send(probe.invite(offer, auth))
            continue
        if code >= 300:
            await client._send(probe.ack_non2xx(msg))
            break
        answer_sdp = body_of(msg) or answer_sdp
        probe.establish_dialog(msg)
        await client._send(probe.request(
            "ACK", probe.remote_uri, probe.cseq, client._generate_branch(), []))
        established = True
        break

    media = media_sections(answer_sdp or "")
    if answer_sdp:
        print(f"\n{'='*60}\nSDP ANSWER\n{'='*60}\n{answer_sdp}")
        Path("last_answer.sdp").write_text(answer_sdp)
        for kind, counter in (("audio", audio), ("video", video)):
            if media.get(kind, {}).get("key"):
                counter.srtp = SrtpContext(media[kind]["key"])
        if write_player_sdp(media, Path("video.sdp"), args.listen):
            print("→ Player SDP written to video.sdp")

    ffplay = None
    if established:
        print(f"\n✓ Call established, listening for RTP for {args.duration:.0f}s...")
        if use_player:
            cmd = player_command(args)
            if cmd and Path("video.sdp").exists():
                print(f"→ Launching: {' '.join(cmd)}")
                ffplay = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
                # Give the player time to bind the relay ports before flushing buffered packets
                loop.call_later(1.5, video.flush)
                loop.call_later(1.5, audio.flush)
                probe.cseq += 1
                loop.call_later(2.0, lambda: asyncio.ensure_future(client._send(probe.request(
                    "INFO", probe.remote_uri, probe.cseq, client._generate_branch(),
                    ["Content-Type: application/media_control+xml"], VFU_BODY))))
            else:
                print("✗ ffplay/ffmpeg not found or no video negotiated")

        talk_task = None
        stop_talk = asyncio.Event()
        conn = re.search(r"^c=IN IP4 (\S+)", answer_sdp or "", re.MULTILINE)
        audio_sec = media.get("audio")
        if (args.talk or args.talk_file) and audio_sec and audio_sec["port"] and conn:
            talk_task = asyncio.create_task(talk(
                args, transports[0], (conn.group(1), audio_sec["port"]),
                int(audio_sec["pts"][0]), audio_key, stop_talk))
        elif args.talk or args.talk_file:
            print("✗ No audio negotiated, cannot talk")

        end = time.monotonic() + args.duration
        while time.monotonic() < end:
            try:
                msg = await client._receive(timeout=0.5)
                first = msg.split("\r\n", 1)[0]
                if not first.startswith("SIP/2.0"):
                    print(f"← Request received: {first}")
                    await client._send(probe.reply_200(msg))
                    if first.startswith("BYE"):
                        print("  → The gateway hung up")
                        established = False
                        break
            except TimeoutError:
                pass

        if talk_task:
            stop_talk.set()
            sent = await talk_task
            print(f"  🎤 {sent} audio packets sent ({sent * 0.02:.1f} s)")

        if ffplay:
            # Stop while RTP still flows, otherwise the demuxer blocks on reads and never sees SIGINT
            await loop.run_in_executor(None, stop_player, ffplay, args)
            ffplay = None

        if established:
            probe.cseq += 1
            await client._send(probe.request(
                "BYE", probe.remote_uri, probe.cseq, client._generate_branch(), []))
            print("→ BYE sent")
            try:
                await client._receive(timeout=2)
            except (TimeoutError, ConnectionError):
                pass

    if ffplay:
        stop_player(ffplay, args)

    print(f"\n{'='*60}\nRESULT\n{'='*60}")
    for c in (audio, video):
        print(f"  {c.name:6s}: {c.packets} packets, {c.bytes} bytes, PT={c.payload_types}, "
              f"SRTP failures={c.auth_failures}")

    for t in transports:
        t.close()
    await client.close()
    return 0 if established or answer_sdp else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
