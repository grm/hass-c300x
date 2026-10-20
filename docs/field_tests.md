# Tests on a real installation — BTicino Door Entry (CLASSE 300X)

> This document records what has been **verified on a real gateway**. The other documents
> (`authentication.md`, `api_endpoints.md`, `device_actions.md`) describe the static analysis of
> the APK. In case of contradiction, this document takes precedence.

Tested installation: C300X gateway on the LAN (`192.168.1.50`), `GatewayId` `1234567`,
one outdoor door station (`DEVADDR`/`where` = `20`). Test client: a Mac on the same LAN
(`192.168.1.20`). Tests carried out in October 2026.

---

## 0. Summary

| Function | Working path | Cloud required? |
|---|---|---|
| Retrieve `PswOpen`, IP, SIP accounts, certificate | Eliot API | **yes**, once only |
| Monitor events | OpenWebNet TCP 20000, HMAC-SHA256 auth with `PswOpen` | no |
| Open the door | **SIP MESSAGE** `*8*19*20##` then `*8*20*20##` | no |
| Open the door via direct OpenWebNet | rejected (`*#*0##`) | — |
| Door station video | **SIP INVITE** + `a=DEVADDR:20`, mandatory SRTP, H264 | no |
| Listen to the door station | same INVITE, Speex 8 kHz, SRTP decrypted in Python | no |
| Talk to the door station | Speex encoded by libspeex (ctypes), SRTP with **our** key | no — validated on site |
| Detect the doorbell | OpenWebNet monitor: `*8*1#1#4#21*10##` | no |
| Receive the doorbell call over SIP | no INVITE received on our local registration | — |

Once the client certificate has been obtained, **everything works locally**: direct TLS to
`<gateway IP>:5061`, without going through `app.sipserver.bs.iotleg.com`.

---

## 1. Eliot cloud (initial setup)

Script: `setup_sip_credentials.py <email>`.

- `GET /eliot/sip/users/plants/{plant}/gateway/{gw}` returns the existing SIP accounts. On
  the tested installation there were two (one per phone that had installed the app). The script
  reuses the first one, without creating any.
- `POST /eliot/users/cert/signcert` returns the **zip as raw binary**, not as Base64 or JSON.
  Decoding the response as UTF-8 crashes (`0xa5 in position 47`). The zip contains
  `<SipAccount>.cert.pem` and `ca-chain.cert.pem`.
- A new RSA key generated locally and signed by the cloud is accepted by the gateway:
  the phone's private key is not needed.

## 2. Local OpenWebNet (TCP 20000)

- Authentication: the gateway requests **HMAC-SHA256** (`*98*2##`). The accepted password
  is **`PswOpen`** (obtained with `need_token: 0`). Implementation: `openwebnet.py` (`OwnHmac`).
- The monitor session (`*99*1##`) works and shows the frames emitted when the app opens the
  door: `*8*19*20##` then `*8*20*20##`. The `where` is **`20`** (`dev` + `addr`), not `200`.
- The same frames sent over a command session are **rejected** (`*#*0##`, NACK) and the
  door does not open. OpenWebNet is therefore only useful for monitoring.
- The doorbell frame has not been identified yet (to be captured with the monitor during a
  press).

## 3. Local SIP/TLS

### 3.1 TLS

- Port `5061`, client certificate mandatory.
- The server certificate is signed by `BsCloud Intermediate CA` (present in `ca-chain`) and
  valid until December 2027.
- Its subject is `CN=sip:1234567.bs.iotleg.com`, **with the `sip:` prefix and no SAN**. Standard
  hostname verification therefore always fails. It must be disabled, while keeping chain
  verification and comparing the CN to `sip:<GatewayId>.bs.iotleg.com`. The app does the same
  thing with `verify_server_cn=0` and `tls_certificate_subject_regexp`.

### 3.2 REGISTER

- The server is `Flexisip/1.0.12 (sofia-sip-nta/2.0)`, embedded in the gateway.
- `REGISTER sip:1234567.bs.iotleg.com` directly receives **`200 Registration successful`**,
  **without a Digest challenge**: the client certificate is enough. The `SipPassword` was not needed.
- The reason phrase is not `OK`. Test the status code, not the text `"200 OK"`.
- The response contains two `Contact` headers, including `<sip:sipserver.bs.iotleg.com;transport=tls>`, and
  `Expires: 300`.

### 3.3 Opening the door — validated

```
MESSAGE sip:c300x@1234567.bs.iotleg.com SIP/2.0
Content-Type: text/plain

*8*19*20##
```

then the same MESSAGE with `*8*20*20##`. The door opens. Test: `test_sip_client.py door 20`.

## 4. Video and audio (INVITE)

Script: `probe_call.py [devaddr] [--ffplay | --record file.mkv|.wav|.jpg] [--talk | --talk-file sound.wav]`.

### 4.1 Offer

```
INVITE sip:c300x@1234567.bs.iotleg.com SIP/2.0
Content-Type: application/sdp

v=0
o=- … IN IP4 <local IP>
s=Talk
c=IN IP4 <local IP>
t=0 0
a=DEVADDR:20                                    ← session attribute: camera selection
m=audio 40000 RTP/SAVP 96 97 98 0 8 101
a=sendrecv
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64 key>
m=video 40002 RTP/SAVP 99 103
a=rtpmap:99 H264/90000
a=fmtp:99 profile-level-id=42801F;packetization-mode=1
a=recvonly
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64 key>
```

- An offer in `RTP/AVP` (unencrypted) receives **`488 Not acceptable here`**. SRTP SDES is
  mandatory.
- There is no Digest authentication on the INVITE either.
- Flow: `100 Trying` → `180 Ringing` → `200 Ok`, then our ACK. The final BYE is accepted.

### 4.2 Gateway answer

```
o=c300x 3550 3234 IN IP4 127.0.0.1
c=IN IP4 192.168.1.50
m=audio 7828 RTP/SAVP 98 101
a=rtpmap:98 speex/8000
a=fmtp:98 vbr=on
a=rtpmap:101 telephone-event/8000
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64 key>
m=video 42508 RTP/SAVP 99
a=rtpmap:99 H264/90000
a=fmtp:99 profile-level-id=42801F
a=sendonly
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<base64 key>
```

- **Audio**: Speex narrowband 8 kHz VBR and RFC 4733 DTMF. Opus, PCMU and PCMA, although offered,
  are not selected.
- **Video**: H264 only. A **VP8-only** offer gets the video rejected (`m=video 0`);
  the call remains established with audio only.
- The gateway's RTP ports change on every call, and RTCP is on port+1 (no
  rtcp-mux).
- RTP starts immediately after the ACK, without us needing to send any packet first.
  The first two packets of each stream have byte 1 equal to `0x01`: these are probably
  STUN requests.
- The answer's `fmtp` does not contain `sprop-parameter-sets`: SPS/PPS arrive in-band in the
  stream. To avoid missing the first keyframe, the client buffers the first packets
  and sends a SIP INFO `application/media_control+xml` (`picture_fast_update`).

### 4.3 Video quality — fixed

`ffprobe` measurements on 8 to 10 s recordings:

| Offer | Resolution | Frames/s | Bitrate |
|---|---|---|---|
| Base (`42801F`, no `b=`) | 400×288 | 10 | ≈ 193 kbit/s |
| `b=AS:2000` | 400×288 | 10 | ≈ 193 kbit/s |
| `b=AS:2000` + `imageattr recv [x=1280,y=720]` | 400×288 | 10 | ≈ 193 kbit/s |
| `b=AS:1000` + `imageattr recv [x=640,y=480]` | 400×288 | 10 | ≈ 194 kbit/s |
| `profile-level-id=42e028` + high `max-br`/`max-fs` + `b=AS:4000` | 400×288 | 10 | ≈ 193 kbit/s |
| `b=AS:64` | 400×288 | 10 | ≈ 197 kbit/s |

- H264 profile: **Constrained Baseline**. The gateway always answers
  `profile-level-id=42801F`, whatever the offer.
- The encoder **ignores any negotiation**, upwards as well as downwards. Quality is
  fixed by the gateway or the door station.
- The app offers no quality setting. Its only lever is `setLowBandwidthEnabled` on a
  slow mobile network (`AbstractC4693c.m22070r`), which most likely has no effect on
  video given the measurements above.
- The `[video] size=qvga` in `linphonerc` only concerns capture on the phone side (disabled),
  not the received stream.

### 4.4 Playback with ffmpeg

ffmpeg's SDP demuxer can decrypt SRTP from an `a=crypto` line. It is enough to
reuse **the gateway's** key (the one from its answer) in a local SDP:

```
v=0
o=- 0 0 IN IP4 127.0.0.1
s=C300X
c=IN IP4 127.0.0.1
t=0 0
m=video 50002 RTP/SAVP 99
a=rtpmap:99 H264/90000
a=fmtp:99 profile-level-id=42801F
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:<gateway's base64 key>
```

```bash
ffplay -protocol_whitelist file,udp,rtp,srtp,crypto -fflags nobuffer -i video.sdp
```

Verified result: sharp image of the door station, with a slight artefact only on the very
first decoded frame.

Since audio was added, `probe_call.py` **decrypts SRTP itself** (`srtp.py`) and relays plain
RTP: audio from 40000 to `127.0.0.1:50000`, video from 40002 to `127.0.0.1:50002`. The local SDP
therefore switches to `RTP/AVP`, without an `a=crypto` line, with `speex/8000` as PT 98 and H264 as PT 99.
This gives a single decryption step, reusable outside ffmpeg (go2rtc, Home Assistant).

- `.mkv` recording: video copied, Speex transcoded to AAC 16 kHz mono. Verified: 7.9 s, H264
  400×288 and AAC playable.
- `.wav` recording: audio only, decoded.
- ffmpeg must be stopped with **SIGINT while RTP is still arriving**, i.e. before the
  BYE. Otherwise the RTP demuxer stays blocked on read, the file is not finalised and the
  duration is `N/A`.
- ffmpeg analyses the stream for about 5 s before writing anything. A recording
  shorter than 6 s may therefore remain empty.

### 4.5 Audio

**Reception (validated).** The gateway sends about 50 packets/s (20 ms per packet), in Speex
narrowband VBR, with 40 to 60 bytes of payload. Decryption with **the gateway's** key
yields 0 authentication failures over hundreds of packets. The decoded sound is consistent: mean
level -41.5 dB, peak -5.8 dB, which matches street noise and voices.
Live listening with `probe_call.py 20 --ffplay` is validated by ear: outdoor noise is
audible.

**Codecs.** Only `speex/8000` is accepted. An offer limited to PCMU/PCMA receives `m=audio 0`:
audio is rejected and only video remains. ffmpeg decodes Speex but **has no Speex
encoder**. To talk, we therefore go through libspeex:

- `custom_components/doorentry/speex.py` uses `ctypes` (`brew install speex` on macOS,
  `libspeex1` on Debian).
- Narrowband mode, quality 8, 160-sample frames (20 ms), i.e. 38 bytes per frame.

**Sending (`--talk`, `--talk-file`).** Packets are sent from the local audio socket (40000),
the one announced in the offer, to `<gateway IP>:<audio port from the answer>`:

- RTP PT 98, marker on the first packet, random initial SSRC, sequence number and timestamp,
  timestamp +160 per frame.
- SRTP with **our key** (the one from our `a=crypto` in the offer), not the gateway's.
- Source: the microphone via `avfoundation` (`--mic 0`), or a file read in real time (`-re`), converted
  by ffmpeg to s16le 8 kHz mono.
- `--talk-delay S` waits S seconds after the call is established before talking.

**Remote validation (half-duplex).** Nobody was at the door station to listen. We therefore
recorded the received audio while sending
(`--talk-file … --talk-delay 8 --record echo.wav`) and measured the RMS level per 250 ms slice:

| File sent (4 s) | Received audio before | During sending | After |
|---|---|---|---|
| Synthetic voice (`test_voix.wav`) | ≈ -50 dB, outdoor noise | **≈ -88 dB for 4 s**, mic muted | -61 dB rising back to -54 dB within a few seconds (automatic gain) |
| Digital silence | ≈ -50 dB | ≈ -50 dB, no change | ≈ -50 dB |

The door station works in **voice-activated half-duplex**. When it receives speech, it
mutes its microphone. It reacts to voice and not to the mere presence of a stream, which shows that it
does decrypt and decode our Speex. See also the on-site test in § 6.3, with the user in front of
the door station during the message. Consequence for the integration: while talking, the visitor cannot be heard,
just like in the app.

**SRTP in Python (`srtp.py`).** AES_CM_128_HMAC_SHA1_80, RTP only (SRTCP is ignored).
The implementation follows RFC 3711:

- KDF with labels 0, 1 and 2.
- IV = salt ⊕ (SSRC ≪ 64) ⊕ (index ≪ 16).
- HMAC-SHA1 tag over packet‖ROC, truncated to 10 bytes.
- ROC estimation per Appendix A.

It is validated against the RFC's B.3 test vectors, as well as by round-trip, sequence-number
rollover and invalid-tag tests.

## 6. Doorbell — test of 2026-10-20

Script: `watch_ring.py <PswOpen>`. It opens an OpenWebNet monitor session, registers a SIP
account (refreshed every 240 s) and logs everything to `ring_log.txt`. On the first unknown
event, it waits 5 s then runs `probe_call.py --talk-file test_voix_x3.wav`.

### 6.1 Frames when the doorbell is pressed

```
15:34:51.144 *#8**35*1*0*0##
15:34:51.437 *8*1#1#4#21*10##          ← call from the door station (doorbell)
15:34:51.566 *#7**31#2*10##
15:34:51.579 *#8**35*2*0*0##
15:34:51.594 *8*9#1#4*20##
15:34:51.602 *7*58#8#0#0#1*##
15:34:51.629 *#8**41*10##
15:34:51.837 *7*77#800#480#2500#148#83#0#800#180#10#15#400#288#0#4000*##   ← video parameters (400×288)
15:34:51.923 *#7**20*50*50*50*75*50##
15:34:51.946 *7*220#0*##
15:34:51.957 *7*300#127#0#0#1#5002#1*##
15:34:51.971 *7*300#127#0#0#1#5007#0*##
15:34:54.755 *#8**35*4*0*0##
15:34:54.776 *7*59#8#0#0*##
15:34:55.480 *7*73#1#100*##
   … unanswered ringing for ≈ 29 s …
15:35:20.602 *7*73#0#0*##
15:35:20.615 *#8**35*0*0*0##
15:35:20.624 *8*3#1#4*420##            ← end of the unanswered call
15:35:20.729 *7*55*##  *7*55*##  *7*219*##  *#8**41*10##  *7*0*##  *7*73#0#0*##  *#7**31#2#0*10##
```

- **Doorbell frame: `*8*1#1#4#21*10##`**. This is the one to watch for the
  "someone is ringing" event in Home Assistant. `*#8**35*1*0*0##` precedes it by 0.3 s, but `*#8**35*…`
  seems to be a general call state (values 0 to 6 observed), not to be used on its own.
- **End of unanswered ringing: `*8*3#1#4*420##`**, about 29 s after the press.
- Opening the gate by hand produced no frame.

Compare with the call we start ourselves to `DEVADDR:20` ("auto-on"):

```
*#8**35*1*0*0##  *8*1#5#4#20*10##  *#8**35*3*0*0##  *8*9#5#4*20##  *7*77#…  *#8**35*6*0*0##
*8*3#5#4*410##   …   *8*2#5#4*10##  *#7**31#5*80##  *8*100#5#4*20##  *7*72#4*20##  *7*53#4000#0*##
end: *#8**35*6*0*0##  *8*3#5#4*410##  *#8**35*0*0*0##  *7*55*##  *7*219*##  *7*0*##
```

In `*8*1#<mode>#4#<address>*10##`, the first parameter distinguishes a ring (`1`) from an
auto-on (`5`). In the doorbell frame, the address is `21` whereas we call the same
door station with `20`. Hypothesis: `2` + `1`, i.e. the type and index of door station 1, probably in
a different format from `DEVADDR`. To be checked with a second door station.

### 6.2 SIP during ringing

- **No INVITE arrives** on our locally registered account, nor any other request. The app
  probably receives the call through the cloud (`sipserver.bs.iotleg.com`, push notification), not
  through a direct registration on the gateway.
- While the door station is ringing, our own INVITE receives **`486 Busy here`** (three attempts
  10 s apart). As soon as ringing ends (`*8*3#1#4*420##`), the INVITE is accepted.
  Consequence: we cannot yet display video while it is ringing. We need either to find
  how to pick up the ongoing call, or to wait for the ringing to end.

### 6.3 Talking after the ring — validated

The synthetic message, played 3 times with 1.5 s pauses (`test_voix_x3.wav`, 15 s), was sent
after the ring:

- The received audio shows **three microphone cut-offs of about 4 s** (at 3.5–6 s, 7.5–11.5 s and
  13–17 s), exactly the rhythm of the message. This is the half-duplex behaviour described in § 4.5.
- The recorded image (`ring_call_4.mkv`) shows the user in front of the door station during
  sending.
- **The user, in front of the door station, heard the message**, though somewhat quiet and with
  some noise; it was understandable. At that time, sending used Speex quality 8,
  without filtering, and packets were paced by `ffmpeg -re`.

### 6.4 Sending improvements (after this test)

- The app has no door station volume setting; it only controls the phone's volume.
  The speaker volume is adjusted on the door station itself.
- The test file was already at maximum (peak -1.1 dBFS, -16 LUFS). To make it sound
  louder, the voice must be **compressed**, not just amplified. The `talk_filter()` filter:
  - keeps the telephone band, 200 to 3400 Hz;
  - applies `acompressor` (threshold -24 dB, ratio 6, makeup +18 dB). Beware: `makeup` is a
    **linear factor** from 1 to 64, not dB;
  - ends with `alimiter` at 0.8, with `level=0`. The headroom is needed because Speex
    decoding makes the peaks overshoot.
- Local loopback measurement (file → filter → Speex → RTP → ffmpeg decoder): **-15.8 → -11.6
  LUFS**, i.e. about +4 dB perceived, with only 1 clipped sample out of 31,500.
- Pacing: packets are sent on our own clock, 20 ms apart, and ffmpeg no longer reads
  the file with `-re`. A burst can overflow the door station's jitter buffer and cause
  clicks.
- Speex moves to quality 10 (instead of 8). Options: `--talk-gain DB` (18 by default) and
  `--speex-quality`.
- Not yet re-tested on site.

## 7. Still open

1. **Picking up during ringing**: the door station answers `486` to our INVITE. Leads: how
   the app receives the call (cloud or push), an OpenWebNet pick-up frame (`*8*2#…`?), or an
   INVITE with other attributes.
2. **Maximum duration of an on-demand call**: the app arms a 15 s timer for call setup;
   the maximum duration of an established call has not been measured precisely.
3. **REGISTER validity**: `Expires: 300`; refreshing is not yet
   implemented in the client.
4. Role of `TVCC` and `CAMERASLIDING`: not tested (a single door station, no CCTV camera).
