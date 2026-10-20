# Field-test scripts

Standalone scripts used while reverse-engineering the BTicino Classe 300X (C300X).
They are independent from Home Assistant. Only `probe_call.py` reuses a few modules
(`srtp`, `speex`) from `custom_components/doorentry/`.

Run them from the repository root. They read and write their files (`sip_credentials.json`,
`sip_certs/`, `video.sdp`, recordings...) in the current directory:

```sh
python3 tools/<script>.py ...
```

## Requirements

- Python 3.9+
- `cryptography` (`pip install cryptography`) for `setup_sip_credentials.py`
- `ffmpeg` / `ffplay` for displaying, recording and sending audio in `probe_call.py`
- `libspeex` (e.g. `brew install speex`) to talk to the door station
- `--talk` captures the microphone through avfoundation, so it only works on macOS.
  `--talk-file` works anywhere ffmpeg does.

## Typical workflow

1. **SIP credentials**: `python3 tools/setup_sip_credentials.py <email> [--plant-id ID] [--device-id ID]`
   logs in to the Eliot cloud (prompts for the password), fetches or creates the SIP account,
   generates an RSA key and CSR and gets the client certificate signed. It writes
   `sip_credentials.json` and `sip_certs/`. **These are secrets.** They are git-ignored, so
   never commit them.

2. **Gateway IP and PswOpen**: `python3 tools/get_password.py [email]` logs in to Eliot and
   prints the gateway info (MAC, PswOpen), the IP read from the configuration zip and a ready-made
   `own_client.py` test command. PswOpen is the OpenWebNet password.

3. **OpenWebNet**: `tools/own_client.py` is an OpenWebNet client (TCP 20000, OPEN and HMAC
   authentication):
   ```sh
   python3 tools/own_client.py test    <IP> [password]               # auth, model code, MAC
   python3 tools/own_client.py monitor <IP> [password] [duration_s]  # print events (press the bell)
   python3 tools/own_client.py open    <IP> <password> <where>       # REALLY opens the door (asks first)
   ```

4. **Video/audio call probe**: `python3 tools/probe_call.py [devaddr]` registers over SIP/TLS,
   sends an INVITE to the door station (`devaddr`, default `20`), prints the SDP offer and answer,
   decrypts SRTP and hangs up after `--duration` seconds (default 15). Main options:
   - `--ffplay`: live video (and audio unless `--no-listen`)
   - `--record FILE`: `.mkv`/`.mp4` (audio+video), `.wav` (audio only) or `.jpg` (one frame)
   - `--talk` / `--talk-file FILE`: send the microphone or an audio file (Speex over SRTP),
     with `--talk-delay`, `--talk-gain` and `--speex-quality`
   - `-v`: print SIP messages. See `--help` for the rest (`--video-bw`, `--imageattr`, `--audio-pts`...).

   Don't run it while the door station is ringing. The gateway answers `486 Busy here` until
   the ringing stops.

5. **Ring watcher**: `python3 tools/watch_ring.py <PswOpen> [--talk-file FILE] [--delay 5] [--call-duration 25] [--minutes 30]`
   opens an OpenWebNet monitor session and registers a SIP account, then waits for a ring. On the
   first unknown OpenWebNet frame or incoming INVITE, it waits `--delay` seconds and runs
   `probe_call.py` to play `--talk-file` and record the call (`ring_call_<n>.mkv`). It makes up to
   4 attempts, 10 s apart, while the door station is still busy. Events are logged to `ring_log.txt`.

`sip_client_pure.py` is the minimal SIP/TLS client (REGISTER, digest auth) used by
`probe_call.py` and `watch_ring.py`.

## macOS firewall

With the macOS application firewall enabled, incoming UDP (the RTP/SRTP media on ports
40000-40003) can be blocked silently for an unsigned Python binary, such as a venv or Homebrew
Python. SIP works but no media packet arrives (`0 packets` in the result). Allow the Python
binary in the firewall settings, or turn the firewall off for the test.
