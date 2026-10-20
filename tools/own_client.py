#!/usr/bin/env python3
"""OpenWebNet test client for the BTicino CLASSE 300X gateway.
Reproduces the behaviour of the Door Entry 1.17.5 app.

Usage:
    python3 tools/own_client.py test <IP> [password]
    python3 tools/own_client.py open <IP> <password> <where>
    python3 tools/own_client.py monitor <IP> [password] [duration_s]
"""
import socket
import hashlib
import re
import sys
import time
from typing import Optional

# OpenWebNet constants (p243h0/AbstractC3790i.java)
ACK = "*#*1##"
NACK = "*#*0##"
CMD_SESSION = "*99*0##"
MON_SESSION = "*99*1##"
HMAC_MODE = "*98*2##"

M32 = 0xFFFFFFFF


def own_calc_pass(password: str, nonce: str) -> str:
    """OPEN algorithm (p227f0/AbstractC3676b.java).
    Tested with own_calc_pass("12345", "603356072") == "25280520"
    """
    pwd = int(password) & M32
    x, started = 0, False
    for c in nonce:
        if c != "0" and not started:
            x, started = pwd, True
        if c == "1":
            x = ((x & 0xFFFFFF80) >> 7) | (x << 25)
        elif c == "2":
            x = ((x & 0xFFFFFFF0) >> 4) | (x << 28)
        elif c == "3":
            x = ((x & 0xFFFFFFF8) >> 3) | (x << 29)
        elif c == "4":
            x = (x << 1) | (x >> 31)
        elif c == "5":
            x = (x << 5) | (x >> 27)
        elif c == "6":
            x = (x << 12) | (x >> 20)
        elif c == "7":
            x = (x & 0xFF00) | ((x & 0xFF) << 24) | ((x & 0xFF0000) >> 16) | ((x & 0xFF000000) >> 8)
        elif c == "8":
            x = ((x & 0xFFFF) << 16) | (x >> 24) | ((x & 0xFF0000) >> 8)
        elif c == "9":
            x = ~x
        x &= M32
    return str(x)


def sha256_hex(data: str) -> str:
    """SHA-256 as lowercase hex."""
    return hashlib.sha256(data.encode()).hexdigest()


def dec2hex(dec_str: str) -> str:
    """Pairs of decimal digits → hex digits (C3675a.m17796a)."""
    result = []
    for i in range(0, len(dec_str), 2):
        result.append(format(int(dec_str[i:i+2]), 'x'))
    return ''.join(result)


def hex2dec(hex_str: str) -> str:
    """Each hex digit → two decimal digits (C3675a.m17797d)."""
    result = []
    for c in hex_str:
        result.append(f"{int(c, 16):02d}")
    return ''.join(result)


class OwnConnection:
    """OpenWebNet connection over TCP 20000."""

    def __init__(self, ip: str, port: int = 20000, timeout: float = 5.0):
        self.ip = ip
        self.port = port
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.buffer = b""
        print(f"✓ Connected to {ip}:{port}")

    def close(self):
        self.sock.close()

    def send(self, frame: str):
        """Sends a frame (appends ## if missing)."""
        if not frame.endswith("##"):
            frame += "##"
        print(f"  → {frame}")
        self.sock.sendall(frame.encode())

    def recv(self, timeout: Optional[float] = None) -> str:
        """Receives a complete frame (up to ##)."""
        if timeout is not None:
            self.sock.settimeout(timeout)

        while b"##" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed by the gateway")
            self.buffer += chunk

        idx = self.buffer.index(b"##") + 2
        frame = self.buffer[:idx].decode('utf-8', errors='replace')
        self.buffer = self.buffer[idx:]
        print(f"  ← {frame}")
        return frame

    def authenticate(self, password: str, session_type: str = CMD_SESSION) -> str:
        """OpenWebNet authentication (AbstractC4933b.java:154-194).
        Returns the method used: 'OPEN', 'HMAC' or 'NONE'.
        """
        # 1. Wait for the welcome ACK
        welcome = self.recv()
        if welcome != ACK:
            raise ValueError(f"Unexpected welcome: {welcome}")

        # 2. Request a CMD or MON session
        self.send(session_type)
        response = self.recv()

        # Case 1: immediate ACK = no auth
        if response == ACK:
            return "NONE"

        # Case 2: NACK = IP not allowed
        if response == NACK:
            raise ValueError("Session refused (NACK) - IP outside the allowed range?")

        # Case 3: HMAC
        if response == HMAC_MODE:
            import uuid
            self.send(ACK)
            ra_frame = self.recv()
            match = re.match(r'\*#(\d+)##', ra_frame)
            if not match:
                raise ValueError(f"Invalid HMAC nonce: {ra_frame}")

            ra_dec = match.group(1)
            ra_hex = dec2hex(ra_dec)

            # Generate Rb and compute the digest
            kab = sha256_hex(password)
            rb_hex = sha256_hex(str(uuid.uuid4()))
            rb_dec = hex2dec(rb_hex)

            A_B = "736F70653E636F70653E"  # A+B constants
            digest_hex = sha256_hex(ra_hex + rb_hex + A_B + kab)
            digest_dec = hex2dec(digest_hex)

            self.send(f"*#{rb_dec}*{digest_dec}")
            confirm_frame = self.recv()

            match = re.match(r'\*#(\d+)##', confirm_frame)
            if not match:
                self.send(NACK)
                raise ValueError(f"Invalid HMAC confirmation: {confirm_frame}")

            # Check the confirmation
            expected = hex2dec(sha256_hex(ra_hex + rb_hex + kab))
            if match.group(1) != expected:
                self.send(NACK)
                raise ValueError("Wrong HMAC confirmation")

            self.send(ACK)
            return "HMAC"

        # Case 4: classic OPEN nonce
        match = re.match(r'\*#(\d+)##', response)
        if not match:
            raise ValueError(f"Unexpected auth response: {response}")

        nonce = match.group(1)
        answer = own_calc_pass(password, nonce)
        self.send(f"*#{answer}")

        ack = self.recv()
        if ack != ACK:
            raise ValueError(f"OPEN authentication refused: {ack}")

        return "OPEN"


def test_connection(ip: str, password: str = "12345"):
    """Basic connection + auth test."""
    print(f"\n=== Connection test to {ip} ===\n")

    try:
        conn = OwnConnection(ip)
        auth_method = conn.authenticate(password)
        print(f"\n✓ Authentication succeeded (method: {auth_method})")

        # Model identification test (AbstractC4933b:195-210)
        print("\n--- Model identification test ---")
        conn.send("*#1013**1")
        model = conn.recv()

        if model.startswith("*#1013*"):
            fields = model.rstrip("#").split("*")
            if len(fields) > 4:
                model_code = fields[4]
                print(f"✓ Model code: {model_code}")
                if model_code == "68":
                    print("  → This is indeed a CLASSE 300X")
                else:
                    print(f"  ⚠ Unexpected model (expected: 68)")

            # Wait for ACK
            conn.recv()

        # MAC read test (AbstractC4933b:213-240)
        print("\n--- MAC read ---")
        conn.send("*#13**12")
        mac_frame = conn.recv()

        if mac_frame.startswith("*#13**12*"):
            fields = mac_frame.rstrip("#").split("*")
            if len(fields) >= 10:
                mac_bytes = []
                for i in range(4, 10):
                    byte_val = int(fields[i])
                    mac_bytes.append(f"{byte_val:02X}")
                mac = ":".join(mac_bytes)
                print(f"✓ MAC: {mac}")

        conn.close()
        print("\n✓ Test succeeded\n")
        return True

    except Exception as e:
        print(f"\n✗ Error: {e}\n")
        return False


def test_door_open(ip: str, password: str, where: str = "20"):
    """Door opening test via OpenWebNet (VctLinphoneService.java:1311-1329).
    WARNING: this will really try to open the door!
    """
    print(f"\n=== Door opening test (WHERE={where}) ===")
    print("⚠ WARNING: this will really try to open the door!")
    confirm = input("Continue? [y/N] ")
    if confirm.lower() != 'y':
        print("Cancelled.")
        return

    try:
        conn = OwnConnection(ip)
        auth_method = conn.authenticate(password)
        print(f"✓ Authenticated ({auth_method})")

        # Send both frames (press + release)
        print(f"\nSending *8*19*{where}##")
        conn.send(f"*8*19*{where}")
        resp1 = conn.recv()

        print(f"Sending *8*20*{where}##")
        conn.send(f"*8*20*{where}")
        resp2 = conn.recv()

        if resp1 == ACK and resp2 == ACK:
            print("\n✓ Commands accepted")
        else:
            print(f"\n⚠ Responses: {resp1}, {resp2}")

        conn.close()

    except Exception as e:
        print(f"\n✗ Error: {e}")


def test_monitor(ip: str, password: str, duration: int = 30):
    """Opens a monitor session and prints the received frames.
    Press the doorbell to see the events.
    """
    print(f"\n=== Monitor session (duration: {duration}s) ===")
    print("Press the doorbell to see the events\n")

    try:
        conn = OwnConnection(ip)
        auth_method = conn.authenticate(password, MON_SESSION)
        print(f"✓ Monitor session open ({auth_method})")
        print("Listening...\n")

        conn.sock.settimeout(1.0)
        start = time.time()

        while time.time() - start < duration:
            try:
                frame = conn.recv()
                print(f"[{time.time()-start:.1f}s] Event: {frame}")
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error: {e}")
                break

        conn.close()
        print("\n✓ Monitor session finished")

    except Exception as e:
        print(f"\n✗ Error: {e}")


if __name__ == "__main__":
    # OPEN algorithm self-test
    assert own_calc_pass("12345", "603356072") == "25280520", "OPEN algorithm broken"

    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} test <IP> [password]")
        print(f"  {sys.argv[0]} open <IP> <password> <where>")
        print(f"  {sys.argv[0]} monitor <IP> [password] [duration_s]")
        print()
        print("Examples:")
        print(f"  {sys.argv[0]} test 192.168.1.42")
        print(f"  {sys.argv[0]} test 192.168.1.42 12345")
        print(f"  {sys.argv[0]} open 192.168.1.42 <password> 20")
        print(f"  {sys.argv[0]} monitor 192.168.1.42 <password> 60")
        sys.exit(1)

    command = sys.argv[1]

    if command == "test":
        ip = sys.argv[2]
        password = sys.argv[3] if len(sys.argv) > 3 else "12345"
        test_connection(ip, password)

    elif command == "open":
        if len(sys.argv) < 5:
            print("Usage: open <IP> <password> <where>")
            sys.exit(1)
        ip = sys.argv[2]
        password = sys.argv[3]
        where = sys.argv[4]
        test_door_open(ip, password, where)

    elif command == "monitor":
        if len(sys.argv) < 3:
            print("Usage: monitor <IP> [password] [duration_s]")
            sys.exit(1)
        ip = sys.argv[2]
        password = sys.argv[3] if len(sys.argv) > 3 else "12345"
        duration = int(sys.argv[4]) if len(sys.argv) > 4 else 30
        test_monitor(ip, password, duration)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
