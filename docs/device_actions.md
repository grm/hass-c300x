# Device actions: door strike, gate, light, video — BTicino Door Entry (CLASSE 300X)

> Source: APK `com.legrandgroup.c300x` v1.17.5, decompiled with JADX into `c300x_src/sources/`.
> Authentication is described in [`authentication.md`](authentication.md), the endpoints and the
> configuration zip in [`api_endpoints.md`](api_endpoints.md).
> Legend: **[C]** = certain (read in the code), **[D]** = deduced, not proven.

## 0. Summary

- **[C] All actions go through SIP**, via the embedded Linphone SDK. There is no REST
  opening command, no OpenWebNet frame over TCP 20000 and no DTMF.
- **[C] Opening the door strike or gate, and stair lighting**: two successive text
  **SIP MESSAGEs** sent to `sip:c300x@<GatewayId>.bs.iotleg.com`. Each body contains an
  OpenWebNet frame:

  | Action | 1st message | 2nd message |
  |---|---|---|
  | Door strike or gate (CID 10060 / 3008) | `*8*19*<where>##` | `*8*20*<where>##` |
  | Stair light (CID 2009) | `*8*21*<where>##` | `*8*22*<where>##` |
  | "Default" door strike (button during a call) | `*8*19*4##` | `*8*20*4##` |

- **[C] Video**: SIP/TLS INVITE with mandatory SRTP, to `c300x`. The app only
  **receives** video (`RecvOnly`). Camera selection uses proprietary SDP attributes
  (`DEVADDR`, `TVCC`, `CAMERASLIDING`).
- **[C] Two SIP registration modes**:
  - **LOCAL**: directly to the gateway, with `;maddr=<gateway IP>`.
  - **REMOTE**: via the flexisip proxy `app.sipserver.bs.iotleg.com`.

```
                 SIP/TLS + client cert + SRTP
 App ── REGISTER ──► C300X gateway (LOCAL, maddr=<IP>)   or   app.sipserver.bs.iotleg.com (REMOTE)
     ── MESSAGE "*8*19*20##" ─┐
     ── MESSAGE "*8*20*20##" ─┴─► sip:c300x@<GatewayId>.bs.iotleg.com   → door strike
     ── INVITE (SDP DEVADDR=20) ─► sip:c300x@…                          → camera on demand
     ◄─ INVITE from c300x@<GatewayId>.bs.iotleg.com                     → call (doorbell)
```

---

## 1. Opening: electric door strike, gate, stair light

### 1.1 Entry points in the user interface

| UI | Code | Device passed |
|---|---|---|
| Home screen: one slider per lock ("Lock") | `p321q2/C4474n.java:789` (list `m854y(plant,"Lock")`, `:1042`) | the `MhpDevice` |
| Home screen: slider during a call | `p321q2/C4474n.java:990` | none, so `where = 4` |
| Call screen (`VctActivityLinphone`) | `activities/VctActivityLinphone.java:498` | none, so `where = 4` |
| "Activations" list (light, generic) | `p321q2/C4464d.java:149-151` | the `MhpDevice` |

The sliders all use `com/legrandgroup/c300x/linphone/C3297c.java`. The Activations list
calls `VctLinphoneService.m15996P` directly. **[C]** There is neither a button in the call
notification nor a home-screen widget to open the door.

### 1.2 Frame construction (`VctLinphoneService.m15996P`, l. 1280-1362)

Pseudo-code of what the method does:

```
require SIP registration state == Ok, otherwise fail
to = "sip:c300x@" + <SIP domain for the current mode>        (§2.1)
CID 10060 (Lock) or 3008 (Generic) → prefixes "*8*19" / "*8*20"
CID 2009 (StairLight)              → prefixes "*8*21" / "*8*22"
any other CID                      → fail
suffix = "*4##" if where is empty, else "*" + where + "##"
send text MESSAGE(prefix1 + suffix) to `to`, then MESSAGE(prefix2 + suffix)
```

- **[C]** `where = deviceDev + deviceAddr`, i.e. the concatenation of the two strings, without a
  separator. If `dev` is null, it defaults to `"2"` (`C3297c.java:69-104`).
- **[C]** The action is only considered successful if **both** messages reach the
  `Delivered` state, i.e. if the gateway replied `200 OK`. The maximum delay is 15 s
  (`VctLinphoneService.java:428-540`).
- **[C]** Before sending, the app checks that its identity ("me") in the ChatRoom is indeed
  `sip:<SipAccount>`. Otherwise it recreates the ChatRoom (`:1343-1347`). `SipAccount` already
  contains `@<GatewayId>.bs.iotleg.com` (`p277l2/C4027F.java:250`).
- **[C]** The behaviour is the same during a call and outside one. Only the `where` changes: a fixed
  `4` during a call, `<dev><addr>` from a device.
- **[D]** `*8*19` / `*8*20` probably correspond to "press / release" of the WHO=8
  command (video door entry), and `*8*21` / `*8*22` to the same pair for the stair light. The code
  does not name these commands.
- **[D]** `dev = "5"` most likely designates a **second entrance**, based on the analytics event
  `vde_doorlock_entr_*` (`:1287-1300`).

### 1.3 Where the addresses come from: the configuration zip

The configuration is downloaded via `GET /eliot/plants/{plantId}/gateway/{gw}/conf`. It is a zip
protected by a password hardcoded in the app (`p074K2/C0594h.java:21`, see `api_endpoints.md` §4).

**`conf.xml` + `current_modality`** (`p074K2/C0596j.java:61-224`):

| Value | Source | Default |
|---|---|---|
| `dev` | `/configuratore/setup/vdes/communication/p_default/dev` | `"2"` |
| `P` | `/configuratore/setup/vdes/communication/p_default/address` | 2nd line of `current_modality` |
| `N` | `/configuratore/setup/vdes/communication/address` | 1st line of `current_modality` |
| `M` | 3rd line of `current_modality`, then `% 10` | – |

Automatically created devices **[C]**:

| Device | CID | `dev` | `addr` | Resulting frame |
|---|---|---|---|---|
| Camera (main door station) | 10050 | `dev` | `P` | INVITE `DEVADDR=<dev><P>` |
| **Main door strike** | 10060 | `dev` | `P` | `*8*19*<dev><P>##` / `*8*20*<dev><P>##` |
| if `M%10 == 0`: stair light | 2009 | `"1"` | `N` | `*8*21*1<N>##` / `*8*22*1<N>##` |
| if `M%10 ∈ 1..3`: additional door strike | 10060 | `"2"` | `P+M` | `*8*19*2<P+M>##` … |
| if `M%10 ∈ 4..6`: additional camera | 10050 | `"2"` | `P+M-3` | INVITE `DEVADDR=2<P+M-3>` |

**[D] Example**: with a single door station, `dev=2` and `P=0` give `*8*19*20##` then
`*8*20*20##`. The actual values depend on each installation's `conf.xml`.

**`archive.xml`** (`p074K2/C0595i.java`, `C0589c.java:120-172`): the `<obj>` and `<ist>` elements
carry the attributes `cid`, `id`, `descr`, `where`, `dev` and `icon`. They become `MhpDevice`s
(`where` → addr, `dev` → dev) when the CID is known (`p067J2/AbstractC0541a.java:162-170`):

| CID | Type | Action |
|---|---|---|
| 10050 | Vde (door station camera) | camera INVITE |
| 10061 | Tvcc (surveillance camera) | camera INVITE + `TVCC=1` |
| 10052 | Intercom | none |
| 10060 | Lock (door strike) | MESSAGE `*8*19` / `*8*20` |
| 3008 | Generic (generic actuator) | MESSAGE `*8*19` / `*8*20` |
| 2009 | StairLight | MESSAGE `*8*21` / `*8*22` |

Other CIDs become `MhpSmartAction`s (scenarios).

### 1.4 What is **not** used

- **[C]** TCP 20000 (`p046G2/C0279f`) is only opened for XOpen association
  (`GWASSOC`/`DEVREP`), release notes (`GETRELNOTES`), firmware status (`*#130**1##`) and
  the discovery probe.
- **[C]** `OpenCmdResponse` (REST) is only an unused response wrapper. No cloud endpoint
  opens the door.
- **[C]** There is neither `sendDtmf` nor SIP INFO (`use_info=0` in `linphonerc_default`).
- **[D] Path to test for Home Assistant**: send `*8*19*<where>##` then `*8*20*<where>##`
  over an authenticated OpenWebNet command session (TCP 20000, `*99*0##`, password `PswOpen`).
  The gateway most likely receives these SCS frames internally, but the APK does not prove
  that it accepts them on this port.

---

## 2. SIP: registration

### 2.1 Server and domain (`VctLinphoneService.m15945A`, l. 597-637)

| Mode | Effective domain | Server address / route |
|---|---|---|
| **LOCAL** | `<GatewayId>.bs.iotleg.com` (if `use_gwid_as_domain`, true by default) | `sip:<domain>;maddr=<gateway IP>;transport=tls` (`:853-858`) |
| **REMOTE** | `<GatewayId>.bs.iotleg.com` | `sip:app.sipserver.bs.iotleg.com;transport=tls`, also used as route (`:953`, `:981-984`) |
| none | `sip.c300x.org` | – |

Mode selection (`m15951T`, l. 732-760) **[C]**:

- The app chooses **LOCAL** if Wi-Fi is connected and the gateway IP is known from
  LAN discovery (`api_endpoints.md` §8). Otherwise it chooses **REMOTE**.
- It forces REMOTE if the screen is off, after more than 2 failures in LOCAL, or if the local
  domain is unknown.

Remote servers (`server_remote` preference): `app.sipserver.bs.iotleg.com` (production),
`app.sipstaging.bs.iotleg.com`, `bs-c3x-sip4-qa.westeurope.cloudapp.azure.com`.

**[D]** The code specifies no port. Linphone therefore uses the standard SIP-TLS port, **5061**, or
a DNS SRV record (`dns_srv_enabled=1`).

### 2.2 Credentials and TLS

| Item | Value | Source |
|---|---|---|
| Identity | `sip:<SipAccount>` (`@domain` added only if missing) | `:955-999` |
| Digest username / password | `SipAccount` / `SipPassword` from `GET /eliot/sip/users/plants/{p}/gateway/{g}` | `p259j0/C3880b.java:62-67` |
| Client certificate | `files/certs/<SipAccount>.cert.rsa` (from signcert) | `AbstractC3314o.java:313-322` |
| CA | `files/certs/<SipAccount>.cert.chain.rsa` (`ca-chain.cert.pem`) | `:205-218` |
| Private key | `files/private` (RSA 2048, generated locally) | `:276-296` |
| Server verification | `sip.tls_certificate_subject_regexp = (sip:<GatewayId>.bs.iotleg.com\|app.sipserver.bs.iotleg.com\|(sipserver\|sipstaging).bs.iotleg.com)` | `:1050-1060` |
| REGISTER expiry | `5184000` s (60 days) | `:955-999` |
| Media encryption | **mandatory SRTP** (`prefs_media_encryption_type="1"`, `..._mandatory=true`) | `:1063-1078` |

In REMOTE mode, the REGISTER Contact also carries the push parameters
`pn-param=door-eliot;pn-provider=fcm;pn-timeout=0;pn-prid=<FCM token>` (`:988-990`).

Without a client certificate, registration in "portal" mode is aborted (`:821-828`).
The manual debug credentials (`user1`/`12345` locally, `dev06.c300x` remotely) are only used
outside portal mode.

### 2.3 Linphone configuration (`res/raw/linphonerc_*`)

| Parameter | Value |
|---|---|
| `sip_port` / `sip_tcp_port` / `sip_tls_port` | `-1` (random local port) |
| `verify_server_certs` / `verify_server_cn` | `1` / `0` |
| `audio_rtp_port` / `video_rtp_port` | `7076` / `9078` |
| `stun_server` | `stun.linphone.org` |
| `max_calls` / `inc_timeout` | `1` / `35` s |
| `[video] size` | `qvga` (320×240) |
| `download_bw` / `upload_bw` | `380` kbit/s |
| `keepalive_period` | `30000` ms |
| Codecs | the Linphone SDK defaults (the app configures none) |

The app disables video capture (`setVideoCaptureEnabled(false)`): the phone never sends
video.

---

## 3. Incoming call (doorbell)

**Caller filter** (`linphone/C3296b.java:744-752`) **[C]**:

```java
String expected = "c300x@" + gateway.getGatewayId() + ".bs.iotleg.com";
if (!call.getRemoteAddress().asStringUriOnly().contains(expected)) call.decline(Reason.Busy);
```

Flow **[C]**:

1. `IncomingReceived`: the app accepts **early media** with audio `Inactive`. The video
   preview is shown before answering (`:881-927`). Playback gain is -100 dB while
   ringing.
2. **Answer** (`C3296b.m16030f`, `:394-435`): `acceptWithParams` with `RecvOnly` video and
   bidirectional audio, plus a 70 s timeout.
3. **Hang up** (`m16043s`, `:651-672`): `decline(Busy)` if the call has not been answered yet,
   `terminate()` otherwise.
4. **Open during the call**: SIP MESSAGE `*8*19*4##` then `*8*20*4##` (§1.2).
5. **Watchdog**: if audio and video bandwidth stay at 0 for more than 10 s, the app
   hangs up and reports error 522 (`:130-161`).

The displayed identity is the display name or the user part of the URI, in practice `c300x`.
**[C]** The URI therefore does not tell which door station is ringing.

During a call, a JSON SIP MESSAGE may arrive with Netatmo presence
(`util/PresenceMessage.java`: `{jsonrpc, params:[{persons:[…]}]}`).

**Push wake-up**: in REMOTE mode, flexisip sends an FCM push. Either the data is empty,
or `loc-args` contains `c300x@<GatewayId>.bs.iotleg.com`. The app re-registers and then receives
the INVITE (`google/VctMessagingService.java:50-79`, `VctLinphoneService.java:210-227`).

---

## 4. On-demand video ("auto-on") and camera switching

### 4.1 Turning on a camera (`p321q2/C4474n.java:669-702` → `C3296b.m16031g`, `:438-526`)

```java
Map<String,String> sdp = new HashMap<>();
sdp.put("DEVADDR", device.dev + device.addr);       // e.g. "20"
if (device.cid == 10061) sdp.put("TVCC", "1");      // surveillance camera
// INVITE sip:c300x@<domain>
params.setVideoEnabled(true); params.setVideoDirection(RecvOnly);
params.setAudioDirection(tvcc ? Inactive : SendRecv);
params.addCustomSdpAttribute("DEVADDR", …); [params.addCustomSdpAttribute("TVCC","1");]
core.inviteAddressWithParams(addr, params);
```

The attributes appear in the session-level SDP as **[D]**:

```
a=DEVADDR:20
a=TVCC:1
```

- No timeout: the call lasts until hang-up.
- The active camera is read from the `DEVADDR` attribute of the remote SDP (`C4474n.java:1164`). The
  first character gives `dev`, the rest gives `addr`.
- A `dev="6"` TVCC camera is a **Netatmo camera**. This is only a hint for
  the user interface.

### 4.2 Switching to the next camera (`C4474n.java:216-222` → `C3296b.m16035k`, `:544-565`)

Re-INVITE (`call.update`) with:

```
a=DEVADDR:<dev><addr of the 1st device in the camera list>
a=CAMERASLIDING:1
```

**[D]** `CAMERASLIDING:1` asks the gateway to switch to the next camera. The app always sends
the `DEVADDR` of the first camera.

### 4.3 Calling the indoor unit ("call home")

`INVITE sip:c300x@<domain>` without any SDP attribute, audio only in `SendRecv`, with a 190 s timeout
(`C3296b.java:478-536`).

### 4.4 Audio controls (local only)

Muting the microphone (`setMicEnabled`), muting the speaker (gain -100 dB) and choosing the output
(speaker or earpiece) are local phone settings (`VctLinphoneService.java:1455-1468`).
**[C]** No "do not disturb" command is sent to the gateway.

---

## 5. Other SIP MESSAGE actions

They are all sent to `sip:c300x@<domain>`, via the same mechanism as the door.

| Action | Body | Code |
|---|---|---|
| Answering-machine status and message list | `GetAswmStatus!` (retried every 2 s until sent) | `p321q2/C4466f.java:101-113` |
| Enable the answering machine | `*8*91##` | `C4466f.java:383` |
| Disable the answering machine | `*8*92##` | `C4466f.java:383` |
| Ask the gateway to send its logs | `*13*35*##` | `VctLinphoneService.java:1529` |

**Answering-machine status response**: it arrives as an incoming SIP MESSAGE, broadcast internally as
`SIP_MESSAGE_RECEIVED` (`myhomeplay/database/VctMessage.java:48-88`):

```
<segment0>;<id>|<mediaType>|<timestamp_s>|<seen>;<id>|…
segment0.split("*")[4] == "1"  → answering machine enabled
seen == "1"                    → message already seen
```

**Playing an answering-machine message**: INVITE to `c300x` with the SDP attributes `MSGID:<id>` and
`MSGTS:<timestamp>`, video enabled (`C4466f.java:303-341`).

---

## 6. Available events

| Source | Event |
|---|---|
| SIP INVITE from `c300x@<GatewayId>.bs.iotleg.com` | **doorbell / incoming call** |
| Incoming SIP MESSAGE | answering-machine status, Netatmo presence |
| FCM push `id_message` | `PLANTCHG`, `FWAVAIL`, `NEWASWMMSG` (new message), `UNTIEUSER`, `UNTIEUSIP` |
| SIP BYE / CANCEL from the caller before answer | missed call |

**[C]** The app does not listen to any OpenWebNet monitor channel (`*99*1##`). It therefore receives no
"door open" or "ringing" event over TCP 20000. **[D]** A monitor session on port
20000 could report the SCS `*8*…` frames and be used to detect the doorbell without SIP. This is to
be tested.

---

## 7. Implementation on the Home Assistant side

### 7.1 Opening the door via SIP (the app's path)

Prerequisites, obtained through the Eliot API (`api_endpoints.md`):

1. `SipAccount` and `SipPassword` (`GET /eliot/sip/users/plants/{p}/gateway/{g}`).
   - If they are missing: `POST /eliot/sip/user`.
   - An account dedicated to Home Assistant counts towards the SIP user limit (error 461
     `MaxSipUsers`).
2. An RSA 2048 key pair and a CSR with `CN=<SipAccount>`, sent to
   `POST /eliot/users/cert/signcert`. This yields `<SipAccount>.cert.pem` and `ca-chain.cert.pem`.
3. The door strike's `where`, read from the configuration zip (§1.3).

Example transaction **[D]** (Linphone sends `text/plain` by default):

```
REGISTER sip:<GatewayId>.bs.iotleg.com SIP/2.0           (TLS → app.sipserver.bs.iotleg.com:5061,
From: <sip:<SipAccount>>                                     or <gateway IP>:5061 locally)
… 200 directly when local: no Digest challenge, the client certificate is enough (field_tests.md §3.2)

MESSAGE sip:c300x@<GatewayId>.bs.iotleg.com SIP/2.0
From: <sip:<SipAccount>>;tag=…
To: <sip:c300x@<GatewayId>.bs.iotleg.com>
Content-Type: text/plain
Content-Length: 10

*8*19*20##
```

The same MESSAGE is then sent with `*8*20*20##`. **Validated on a real installation** locally
(`field_tests.md` §3.3) with a minimal `asyncio` SIP client (`sip_client_pure.py`).

Possible libraries: **pjsua2** (Python bindings, TLS with client certificate, SRTP),
**baresip** (`ctrl_tcp` module), or **liblinphone** (the same SDK as the app). For a simple MESSAGE,
a minimal TLS and SIP stack in `asyncio` may be enough. The app requires a prior REGISTER; it remains to
be checked whether the server accepts an out-of-dialog MESSAGE without REGISTER, authenticated only by
digest.

### 7.2 Video

- A SIP client capable of INVITE over TLS and of handling **SRTP** (SDES) is required. The gateway
  only sends **H264**; audio is **Speex 8 kHz** (verified, `field_tests.md` §4).
- For a `camera` entity in Home Assistant, one option is to run baresip or
  pjsua as an RTP relay to ffmpeg/go2rtc, with an `a=DEVADDR:<dev><addr>` INVITE triggered on
  demand.
- The received stream is **400×288 at 10 frames/s, about 190 kbit/s**, fixed regardless of the SDP offer
  (`field_tests.md` §4.3). The `qvga` in `linphonerc` only concerns capture.

### 7.3 Local paths to test (not used by the app)

| Path | How | Benefit |
|---|---|---|
| Opening via OpenWebNet | TCP 20000 CMD session + `PswOpen`, then `*8*19*<where>##` and `*8*20*<where>##` | **tested: rejected** (`*#*0##`) |
| Doorbell via OpenWebNet | MON session `*99*1##`, observe the `*8*…` frames on a press | call detection without SIP |
| Local SIP | direct REGISTER on `<IP>:5061` (domain `<GatewayId>.bs.iotleg.com`) | **tested: works** for opening and video |

---

## 8. Unresolved points

1. **[D]** The exact semantics of `*8*19`, `*8*20`, `*8*21`, `*8*22`, `*8*91`, `*8*92` and
   `*13*35*`. The code only names the CIDs and the analytics events.
2. **[D]** The role of `TVCC` and `CAMERASLIDING` on the gateway side.
3. ~~The SIP-TLS port and the MESSAGE Content-Type~~: `5061` and `text/plain`, verified.
4. The "video early media" branch is poorly decompiled (`C3296b.java:890`). The exact preview
   parameters are not readable.
5. This document is a **static** analysis of the APK. The results of the tests on a real
   installation are in `field_tests.md`, which takes precedence in case of contradiction.
