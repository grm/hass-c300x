# Authentication — BTicino Door Entry CLASSE300X

> Reverse engineering of the Android application **Door Entry CLASSE300X**
> (`com.legrandgroup.c300x`, version **1.17.5**, versionCode 217), decompiled with JADX
> into `c300x_src/`.
> APK signed by `CN=BTicino, OU=Mobile Development, O=BTicino, C=IT`
> (SHA-256 `99:6A:05:B9:2C:9F:B3:BB:2F:00:08:BF:E9:AB:F3:13:C9:33:52:99:6D:C3:18:0E:2C:85:FB:68:38:5B:74:3F`).
>
> The class/method names cited (`p259j0/C3896r.java`, …) are those produced by JADX
> (obfuscated code). Paths are relative to `c300x_src/sources/`.

## 1. Overview: Eliot cloud **and** local

The application is **hybrid**. The channel manager `p046G2/C0279f.java`
(log tag `MhpSocketLinkmng`) opens three types of connections
(`p046G2/EnumC0280g.java`):

| Channel       | Transport                         | Protocol                        | Authentication                                       | Code                                         |
|---------------|-----------------------------------|---------------------------------|------------------------------------------------------|----------------------------------------------|
| `Portal`      | HTTPS to `www.myhomeweb.com:443`  | "Eliot" REST/JSON               | Login/password → `auth_token` header                 | `p046G2/AsyncTaskC0278e.java`, `p259j0/*`    |
| `Managing`    | TCP **20000** on the LAN          | **XOpen** (OpenWebNet wrapped in XML) | OPEN password ("OPEN" hash or HMAC-SHA256)     | `p046G2/AsyncTaskC0281h.java`, `p243h0/C3800s.java` |
| `Operativing` | TCP **20000** on the LAN          | raw **OpenWebNet** (`*…##`)     | OPEN password ("OPEN" hash)                          | `p046G2/AsyncTaskC0277d.java`, `p243h0/C3792k.java` |

In addition to these channels, audio/video goes through **SIP (Linphone, `liblinphone.so`)** with
SIP credentials and a client certificate provided by the cloud (see §4 and `device_actions.md`).

> In v1.17.5, the `Operativing` channel is never opened. `Managing` is only used for
> association and release notes. **Actions** (door, light, camera) all go
> through **SIP**: see `device_actions.md`.

The **Netatmo** SDK (OAuth2) is embedded but **neutralised** in this application (see §6):
the cloud used is indeed **Legrand-BTicino's Eliot / MyHomeWeb**, not the Netatmo API.

```
           ┌──────────────── Legrand "Eliot" cloud (www.myhomeweb.com) ──────────────────┐
 App ──────┤ 1. POST /eliot/users/sign_in   → auth_token header                          │
           │ 2. GET  /eliot/plants/.../gateway/{gw}  (need_token=0) → PswOpen             │
           │ 3. POST /eliot/sip/user, POST /eliot/users/cert/signcert → SIP account + cert│
           └──────────────────────────────────────────────────────────────────────────────┘
 App ── LAN TCP:20000 ── C300X gateway (OpenWebNet / XOpen, OPEN password)
 App ── SIP (TLS, client cert) ── SIP server / door station (audio, video)
```

---

## 2. Eliot cloud (MyHomeWeb portal)

### 2.1 Base URL and environments

`com/legrandgroup/c300x/VctSettings.java:123-139` (`m15826K1` / `m15827L1`):

| `prefs_portal_address` preference | Base URL                                                                        |
|-----------------------------------|---------------------------------------------------------------------------------|
| *(default / production)*          | `https://www.myhomeweb.com`                                                     |
| `PORTAL_STAGING`                  | `https://www.sta.myhomeweb.com`                                                 |
| `PORTAL_STABLE`                   | `https://stable-eliot-mule-balancer-2146709889.eu-west-1.elb.amazonaws.com`     |
| `PORTAL_UNSTABLE`                 | `https://unstable-eliot-mule-balancer-1963338202.eu-west-1.elb.amazonaws.com`   |

Port: `prefs_portal_port` preference, default **443**.
Requests are built as follows (`p259j0/AbstractC3879a.java`, all `p259j0/C38xx` classes):

```
<base> + ":" + <port> + "/eliot/..."      →  https://www.myhomeweb.com:443/eliot/users/sign_in
```

("mule" in the AWS load balancer names suggests a MuleSoft backend — inference.)

### 2.2 HTTP transport

HTTP client: Apache `DefaultHttpClient` (`p275l0/C4019b.java`, `p275l0/AbstractC4018a.java`).

- **TLS**: system trust store + `STRICT_HOSTNAME_VERIFIER`, **no certificate pinning**
  (`AbstractC4018a.java:41-101`). A MITM proxy with a CA installed system-wide is therefore
  enough to observe the traffic.
- Connection timeout: 10 s, HTTP/1.1, UTF-8 charset.
- **Body**: the value of the internal `payload` parameter is sent **raw** as the body
  (`ByteArrayEntity`) for POST/PUT (`C4019b.java:165-197`). For GET, any parameters
  are encoded as a query string (`C4019b.java:203-215`).
- No specific `User-Agent` is set.
- The response body is stored under `payload`, the **response headers** in a separate
  Bundle (`bticino/btcommlib/domain/restclient/request/Response.java`, `m12089b`).
- The app's logs mask `pwd`, `PswOpen`, `SipPassword` and `Password`
  (`C4019b.java:155`): these are the protocol's four secrets.

### 2.3 Common headers

| Header         | Value                     | Present                                        |
|----------------|---------------------------|------------------------------------------------|
| `prj_name`     | `C3X`                     | all requests                                   |
| `Content-Type` | `application/json`        | requests with a body                           |
| `auth_token`   | token obtained at sign-in | all requests except `sign_in`, account creation and `pwd-reset` |

⚠️ The token is passed in a bare **`auth_token`** header — **not** `Authorization: Bearer`.

### 2.4 Login — `POST /eliot/users/sign_in`

Request: `p259j0/C3896r.java:11-17`, model `p259j0/C3888j.java`.

```http
POST /eliot/users/sign_in HTTP/1.1
Host: www.myhomeweb.com:443
Content-Type: application/json
prj_name: C3X

{
  "username": "user@example.com",
  "pwd": "<Door Entry account password>",
  "appVersion": "legacy-doorentryforc300x-android-1.17.5",
  "registrationId": "<FCM token, optional>"
}
```

- `appVersion` = `"legacy-"` + `"doorentryforc300x-android-"` + `versionName`
  (`C3888j.m18701c`, called from `p277l2/AbstractC4039h.java:49` and `p277l2/C4057z.java:143`).
- `registrationId` = `prefs_gcm_token` preference (Firebase Cloud Messaging token), added
  only if non-empty (`C3888j.java:79-83`, `VctSettings.m15820E1`).
  For an integration without push, it can be omitted.

**Response** — `bticino/btcommlib/domain/restclient/btwebportal/SignResponse.java`:

- The session token is read from the **`auth_token` HTTP response header**
  (`response.m12091d().getString("auth_token")`; `m12091d()` = the headers Bundle).
  It is then stored globally (`p259j0/C3884f.m18685e`) and injected into all
  subsequent requests.
- The body (`payload`) is parsed into the same `C3888j` model; fields read:
  `username`, `pwd`, `RemainingTries`, `RemainingMinutes`, `registrationId`, `appVersion`.
  `RemainingTries` / `RemainingMinutes` most likely indicate an anti-bruteforce lockout
  after failures (inferred from the names).

Minimal Python example:

```python
import requests

BASE = "https://www.myhomeweb.com:443"
r = requests.post(
    f"{BASE}/eliot/users/sign_in",
    headers={"Content-Type": "application/json", "prj_name": "C3X"},
    json={
        "username": EMAIL,
        "pwd": PASSWORD,
        "appVersion": "legacy-doorentryforc300x-android-1.17.5",
    },
    timeout=10,
)
r.raise_for_status()
auth_token = r.headers["auth_token"]
session_headers = {"auth_token": auth_token, "prj_name": "C3X"}
```

### 2.5 Logout — `POST /eliot/users/sign_out`

`p259j0/C3896r.java:20-25` — headers `Content-Type: application/json`, `auth_token`,
`prj_name: C3X`, no body.

### 2.6 Token lifetime and re-authentication

- **No refresh token** and no client-side expiry date.
- **Possible token rotation**: the `UserResponse` (l. 21-25), `C3xSipUserResponse`
  (l. 15-19), `C3xTLSResponse` (l. 14-18) and `SignResponse` wrappers replace the stored token if
  the response contains an `auth_token` header. A client must therefore always use the latest
  `auth_token` received.
- On an **HTTP 401** error (`e_Unauthorized`), the `VctAutoLoginController` controller
  (`p277l2/AbstractC4039h.java:109-118`) reloads the user saved in the database (`MhpUser`)
  and **simply replays `sign_in`** with login/password.
- Recommended strategy for Home Assistant: keep email + password in the config
  entry, do a `sign_in` at startup, and redo a `sign_in` on every 401.

### 2.7 Result codes

Enumeration `p243h0/C3785d.java:35-89` (real HTTP codes + application codes):

| Code | Name                        | | Code | Name                         |
|-----:|-----------------------------|-|-----:|------------------------------|
| 200  | `e_NoError`                 | | 434  | `e_UserDisabled`             |
| 209  | `e_passwordExpired`         | | 435  | `e_EmailAlreadyUsed`         |
| 210  | `e_contractExpired`         | | 436  | `e_UserAlreadyAssociated`    |
| 211  | `e_userComingFromMyHomePlay`| | 437  | `e_SipUserAlreadyPresent`    |
| 400  | `e_BadRequest`              | | 441  | `e_TokenNotValidated`        |
| 401  | `e_Unauthorized`            | | 450  | `e_GatewayAlreadyAssociated2`|
| 404  | `e_NotFound`                | | 460  | `e_GatewayNotPresented`      |
| 432  | `e_InvalidPassword`         | | 461  | `e_MaxSipUsers`              |
| 433  | `e_PasswordAlreadyUsed`     | | 500 / 503 | server error / unavailable |

Codes ≥ 990 and 1000-3025: app-internal errors (network, SSL, socket…), never returned
by the server.

### 2.8 Account management (for reference)

`p259j0/C3899u.java` — `/eliot/user`: `GET` (profile), `POST` (creation, without `auth_token`),
`PUT` (update, e.g. `{"Password": "..."}`), `DELETE`; `POST /eliot/user/pwd-reset`
(without `auth_token`). Details in `api_endpoints.md`.

---

## 3. Local gateway secret (OPEN password)

The local sockets (port 20000) require the C300X gateway's **OPEN password**.
The application does not ask the user for it: it retrieves it **from the cloud**.

### 3.1 Retrieval via Eliot

`p259j0/C3890l.java:39-48` (`m18717d`):

```http
GET /eliot/plants/{plantId}/gateway/{gatewayId} HTTP/1.1
auth_token: <token>
prj_name: C3X
need_token: 0                            ← at login / alignment (p277l2/C4023B.java:46)
```

Variant `need_token: 1` + `mac_address: <MAC>`: used **only** when
replacing/associating a gateway (`p277l2/C4028G.java:49`).

Response parsed by `p259j0/C3889k.java:62-73` (`MhPlayGw`):

| JSON field       | Type   | Role                                                            |
|------------------|--------|-----------------------------------------------------------------|
| `MacAddress`     | string | gateway MAC                                                     |
| `GatewayId`      | string | gateway identifier                                              |
| `Token`          | string | gateway token                                                   |
| `TemporaryToken` | string | temporary token (replaced by `Token` if empty)                  |
| `PswOpen`        | string | the gateway's **OPEN password**                                 |
| `Associato`      | long   | association flag/identifier (`> 0` = already associated)        |

Usage:
- The `MhpGateway(C3889k)` constructor copies **`PswOpen`** as the local password
  (`com/legrandgroup/myhomeplay/database/MhpGateway.java:161`).
- When associating a new installation (`p385y2/C5017c.java:300-316`,
  `VctAssociateNewPlantProcedure`), it is **`TemporaryToken`** (or `Token` if empty) that is
  stored as the gateway password.

👉 To be checked on a real installation: which of `PswOpen` / `Token` is accepted by
the gateway for OpenWebNet authentication.

This response does **not** contain the gateway's LAN IP. The IP comes from the configuration
zip (`GET …/gateway/{gw}/conf`, file `read-only-par.txt`) or from network discovery
(see `api_endpoints.md` §4 and §8).

### 3.2 Default value

If no gateway is known or if its password is empty, the application uses
**`12345`**, BTicino's factory OPEN password (`p060I2/C0475a.java:180,193,231,472`).

### 3.3 Storage in the app

Local SQLite database, `token` column, encrypted via `VctSecurity`
(`p039F2/C0232b.java:169` → `com/legrandgroup/c300x/util/AbstractC3314o.m16174b`).
A "security migration" rewrites the old format (`security_decrypt_legacy`).

---

## 4. SIP credentials and TLS client certificate

Only needed for video/audio (details in `device_actions.md`).

1. **SIP account** — `p259j0/C3881c.java`:
   - `POST /eliot/sip/user` (creates a SIP account for this phone), body
     `{"SipAccount","DeviceName","GatewayId","IdDevice"}` (`p259j0/C3880b.java:139-142`);
   - `GET /eliot/sip/users/plants/{plantId}/gateway/{gatewayId}` (list).
   - Response (`C3880b.java:62-67`): `SipAccount`, `DeviceName`, `GatewayId`, `IdDevice`,
     **`Username`**, **`SipPassword`**.
2. **Client certificate** — `com/legrandgroup/c300x/util/AbstractC3314o.java:166-200,255-256`:
   - generation of an **RSA 2048** key pair;
   - **SHA256withRSA** CSR, subject
     `C=IT, ST=Italy, L=Varese, O=Bticino spa, OU=DSI-PQS, CN=<SipAccount>`;
   - submission: `POST /eliot/users/cert/signcert` with `{"CommonName": "<SipAccount>", "CSR": "<PEM on one line>"}`
     (`p259j0/C3883e.java`, `p259j0/C3882d.java:44-45`); chained in
     `p277l2/C4027F.java:220-227`;
   - the (binary) response is **Base64-encoded** by the HTTP client (special case of the
     `TLS` request type, `C4019b.java:86`). It is an **unencrypted zip**
     (`X-AttachmentName` / `X-AttachmentMD5` headers) containing `<SipAccount>.cert.pem` and
     `ca-chain.cert.pem` (`p277l2/C4027F.java:162-176`). The app keeps the chain in
     `files/certs/<SipAccount>.cert.chain.rsa`.
3. **SIP account format** (`p277l2/C4027F.java:244-250`):
   ```
   SipAccount = <email with "@" → "-"> + "-" + <IdDevice> + "@" + <GatewayId> + ".bs.iotleg.com"
   DeviceName = Build.MANUFACTURER + " " + Build.MODEL
   IdDevice   = phone's Wi-Fi MAC, uppercase and without ":" (VctSettings.m15860y1).
                If Android masks the MAC (02:00:00:00:00:00) and /sys/class/net/<if>/address
                is unreadable: random UUID "installation_id" (AbstractC4933b.java:341-370)
   ```
   If Wi-Fi has never been active, reading the MAC throws an exception and the app displays "Cannot get MAC address, Enable Wi-Fi at least once".
   For Home Assistant, any stable identifier seems to be fine, since a UUID is accepted.
   Default SIP server: `app.sipserver.bs.iotleg.com` over TLS (`VctSettings.java:73-80`).

---

## 5. Local OpenWebNet / XOpen authentication (TCP 20000)

### 5.1 Connection parameters

`p046G2/C0279f.java:83,103`:

```java
// Managing   → XOpen
new AsyncTaskC0281h().execute(gatewayIp, "20000", openPassword, "false" /*IS_SSL*/, "false" /*IS_SSL_PROD*/);
// Operativing → OpenWebNet
new AsyncTaskC0277d().execute(gatewayIp, "20000", openPassword);   // MODE = "RW"
```

- IP: gateway discovered on the LAN (`p060I2/C0475a.m1586e`, see `api_endpoints.md`).
- **SSL: not supported** — `C3800s.m18355d` returns `false` if `IS_SSL == true`. Everything
  happens over **plain TCP**.

### 5.2 Control frames (`p243h0/AbstractC3790i.java`)

| Constant   | Frame           | Meaning                                      |
|------------|-----------------|----------------------------------------------|
| ACK        | `*#*1##`        | acknowledgement                              |
| NACK       | `*#*0##`        | rejection                                    |
| CMD        | `*99*0##`       | **command** session request                  |
| MON        | `*99*1##`       | **monitor** session request (events)         |

### 5.3 Classic OpenWebNet handshake (`p243h0/C3792k.java:72-118`, tag `BTOpenLink`)

```
GW  → *#*1##                       (initial ACK, timeout 30 s)
App → *99*0##   (CMD)  or  *99*1## (MON)
GW  → *#*1##                       → connected without password (authorised IP)
   | *#*0##                        → rejected
   | *#<nonce>##                   → challenge
App → *#<OPEN(password, nonce)>##
GW  → *#*1##                       → authenticated
```

If the final response matches `.*\*#[0-9]([0-9])+##`, the app raises `BT_CONN_ERR`
("IP not in range").
The `Operativing` channel (`C3792k`) only handles the "OPEN" algorithm.

**OpenWebNet HMAC variant (`*98*2##`)** — implemented in the LAN discovery probe
(`p377x2/AbstractC4933b.java:154-193`), with the same computations as §5.5:

```
GW  → *#*1##
App → *99*0##
GW  → *98*2##                       → HMAC-SHA256
App → *#*1##
GW  → *#<Ra>##                      (Ra hex-as-decimal encoded)
App → *#<Rb>*<Digest>##             (Rb and Digest hex-as-decimal encoded)
GW  → *#<Confirm>##                 → verified by the app, otherwise it sends *#*0##
App → *#*1##
```

If the response to `*99*0##` is not `*98*2##`, the probe treats it as `*#<nonce>##` and
replies with the OPEN algorithm. A Home Assistant client must handle **both** variants.

### 5.4 "OPEN" algorithm (`p227f0/AbstractC3676b.java`, tag `BTOpenPassword`)

Non-cryptographic 32-bit hash: each digit of the nonce applies a rotation/permutation
to the password, which must be **numeric** (`Long.valueOf`). The control flow produced by
JADX is partially broken; the transcription below reproduces the semantics of each
branch and has been validated against the public test vector `password=12345`, `nonce=603356072`
→ `25280520`.

```python
M = 0xFFFFFFFF

def own_calc_pass(password: str, nonce: str) -> str:
    pwd = int(password) & M
    x, started = 0, False
    for c in nonce:
        if c != "0" and not started:          # the password is loaded at the first non-zero digit
            x, started = pwd, True
        if c == "1":   x = ((x & 0xFFFFFF80) >> 7) | (x << 25)
        elif c == "2": x = ((x & 0xFFFFFFF0) >> 4) | (x << 28)
        elif c == "3": x = ((x & 0xFFFFFFF8) >> 3) | (x << 29)
        elif c == "4": x = (x << 1) | (x >> 31)
        elif c == "5": x = (x << 5) | (x >> 27)
        elif c == "6": x = (x << 12) | (x >> 20)
        elif c == "7": x = (x & 0xFF00) | ((x & 0xFF) << 24) | ((x & 0xFF0000) >> 16) | ((x & 0xFF000000) >> 8)
        elif c == "8": x = ((x & 0xFFFF) << 16) | (x >> 24) | ((x & 0xFF0000) >> 8)
        elif c == "9": x = ~x
        x &= M
    return str(x)

assert own_calc_pass("12345", "603356072") == "25280520"
```

### 5.5 XOpen handshake (`p243h0/C3800s.java:96-200`, tag `BTXOpenLink`)

XOpen = XML messages (namespace `http://www.bticino.it/xopen/v1`, root `OWNxml`,
serialisation `p291n0/C4175m.java:93-110`) over the same TCP 20000 socket.

```
GW  → *#*1##
App → <OWNMsg Profile="V4"><Payload><Service TYPE="SET"/><SeqID ID="0" Progress="0"/><Address/>
        <ActionID>OWNSetProtocol</ActionID><Params><Param name="Version">2</Param>
        <Param name="Command">1</Param><Param name="Monitor">0</Param>
        <Param name="ConnAlwaysOn">1</Param></Params></Payload></OWNMsg>
GW  → <OWNxml …><Hdr>…</Hdr><Cmd><WMsg> <PwdOp>…</PwdOp> <AuthMode>…</AuthMode> <Random>…</Random> </WMsg></Cmd></OWNxml>
App → <OWNxml …><Hdr>…MsgID+1…</Hdr><Cmd Dsc="Reply Welcome Message"><RWMsg>…</RWMsg></Cmd></OWNxml>
GW  → ACK   (AuthMode ≠ 2)
    | <CWMsg><Digest>…</Digest></CWMsg>   (AuthMode = 2, then the app sends back an ACK code 200)
```

Fields (`p291n0/C4171i.java`, `C4170h.java`, `C4167e.java`):

| Message | Field      | Contents                                                          |
|---------|------------|-------------------------------------------------------------------|
| `WMsg`  | `PwdOp`    | numeric nonce for the OPEN algorithm                              |
| `WMsg`  | `AuthMode` | `2` = HMAC-SHA256, otherwise OPEN                                 |
| `WMsg`  | `Random`   | server nonce **Ra** ("hex-as-decimal" encoded, see below)         |
| `RWMsg` | `PwdRes`   | (OPEN mode) `own_calc_pass(password, PwdOp)`                      |
| `RWMsg` | `Digest`   | (HMAC mode) client HMAC                                           |
| `RWMsg` | `Random`   | (HMAC mode) client nonce **Rb**                                   |
| `CWMsg` | `Digest`   | (HMAC mode) server confirmation                                   |

The response header (`Hdr`) swaps `Src`/`Dst` and sets `MsgID` = received MsgID + 1
(`C3800s.m18348l`).

**HMAC mode (`AuthMode = 2`)** — `p227f0/C3675a.java`:

"Hex-as-decimal" encoding: each hexadecimal character is written as 2 decimal digits
(`a` → `10`, `f` → `15`, `3` → `03`).

```python
import hashlib, uuid

def dec2hex(s: str) -> str:            # C3675a.m17796a
    return "".join(format(int(s[i:i+2]), "x") for i in range(0, len(s), 2))

def hex2dec(h: str) -> str:            # C3675a.m17797d
    return "".join(f"{int(c, 16):02d}" for c in h)

sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

A, B = "736F70653E", "636F70653E"      # "sope>" / "cope>" as ASCII hex

def hmac_reply(ra_dec: str, password: str):
    ra  = dec2hex(ra_dec)
    kab = sha(password)
    rb  = sha(str(uuid.uuid4()))
    digest = sha(ra + rb + A + B + kab)
    expected_confirm = hex2dec(sha(ra + rb + kab))
    return {"Digest": hex2dec(digest), "Random": hex2dec(rb)}, expected_confirm
```

The `CWMsg` `Digest` must equal `expected_confirm` (`C3675a.m17800c`). Note: if this
check fails, the app merely logs
`"HMAC Confirmation Code failed!"` and still considers the connection established
(`C3800s.java:133-140`).

---

## 6. Netatmo OAuth2 (present but inactive)

The app embeds the Netatmo SDK (`com/netatmo/*`) and AppAuth (`net/openid/appauth/*`):

- Endpoints built from a base URL: `<base>/oauth2/authorize`,
  `<base>/oauth2/token`, `<base>/oauth2/revoke` (`p172Y2/C1269E.java:44-77`).
- Implemented grants (`p130S2/C1000c.java`): `password` (with optional `user_prefix` and `scope`),
  `refresh_token`, revocation (`token_type_hint=refresh_token`), and the
  `authorize?access_token=…&target_client_id=…` exchange (SSO between apps).
- AppAuth redirect URI: scheme `com.legrandgroup.c300x:` (`AndroidManifest.xml:158-165`).

**However**, the configuration injected by the app (`p011B2/C0080c.java:96-114`) returns
`client_id = ""`, `client_secret = ""` and a base URL of `""`. This module is therefore not used
for this application's authentication: it is inherited from the Netatmo "kit"
(webviews, marketing messaging). **No usable Netatmo key is present.**

## 7. Other keys found (unrelated to auth)

`res/values/strings.xml`: `google_api_key` / `google_crash_reporting_api_key` (Firebase),
`firebase_database_url = https://door-eliot.firebaseio.com`, `default_web_client_id`
(Google Sign-In). These are public Firebase/Google configuration keys, useless for
controlling the intercom.

---

## 8. Implications for the Home Assistant integration

1. **Config flow**: Door Entry account email + password → `sign_in` →
   `auth_token` header.
2. List the installations and gateways (`api_endpoints.md`).
   - Retrieve `PswOpen` and the MAC via `GET /eliot/plants/{plant}/gateway/{gw}` with
     `need_token: 0`.
   - Retrieve the IP via the configuration zip (`api_endpoints.md` §4).
3. **Actions (door, light)**: the app only uses **SIP MESSAGE** over a SIP/TLS
   REGISTER. The SIP account (`SipAccount`/`SipPassword`) and the client certificate (§4) are therefore required.
   See `device_actions.md`.
4. ~~Local path~~: the same frames (`*8*19*<where>##`) sent over a TCP 20000 command session
   are **rejected** (`*#*0##`). OpenWebNet is only used for monitoring.
5. **Video**: same SIP account. See `device_actions.md`.
6. Cloud re-authentication: redo `sign_in` on every 401.

### Unverified points

- Exact format of the `sign_in` response body (the code only reads the `auth_token` header
  with certainty).
- ~~Password accepted on port 20000~~: **`PswOpen`**, verified.
- ~~`AuthMode` of a recent firmware~~: the tested gateway requests **HMAC-SHA256** (`*98*2##`).
- The local SIP REGISTER does **not** require Digest: the client certificate is enough.
- The results of the tests on a real gateway are in `field_tests.md`.
