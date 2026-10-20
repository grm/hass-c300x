# Eliot cloud endpoints, JSON structures and TCP sockets — BTicino Door Entry (CLASSE 300X)

> Source: APK `com.legrandgroup.c300x` v1.17.5 (SHA-256
> `e323291e95b67a264169b0ce30264de39504202fd231b949737a77eaf9b3576f`), decompiled with JADX into
> `c300x_src/sources/`. The `pXXX/CYYYY.java:line` paths are relative to that folder.
> Authentication (sign_in, token, OPEN/HMAC, SIP certificate) is described in
> [`authentication.md`](authentication.md). Door-opening commands and video are described in
> [`device_actions.md`](device_actions.md).

## Key findings

| # | Finding | Certainty |
|---|---|---|
| 1 | **No cloud endpoint can open the door.** The type `EnumC3895q.OpenMsg` is mapped to `OpenCmdResponse` (`C2546a.java:52-53`), but no request builder uses it. The app opens the door via **SIP MESSAGE** (see `device_actions.md`). | certain |
| 2 | **No JSON endpoint returns the gateway's LAN IP.** The only cloud source is `read-only-par.txt`, inside the configuration zip (§4). Otherwise, the IP comes from a subnet scan (§8). | certain |
| 3 | E-mail notification registration is disabled in this version (`C4046o.java:161`, "Temporary skip"). | certain |
| 4 | No UDP, multicast, mDNS or SSDP discovery. UPnP is disabled in the code. | certain |
| 5 | Firebase is only used for FCM (push). The Realtime Database is not embedded. | certain |

---

## 1. Common HTTP transport

| Item | Value | Source |
|---|---|---|
| Base | `https://www.myhomeweb.com` (prod, default) | `VctSettings.java:123-135` |
| Port | `443` | `VctSettings.java:138-140` |
| Prefix | `<base>:<port>/eliot/…` | `p259j0/AbstractC3879a`, `p259j0/C3884f` |
| Client | Apache `DefaultHttpClient`, system certificate store, no pinning | `p275l0/AbstractC4018a`, `p275l0/C4019b` |

Other environments, selected by the `prefs_portal_address` preference:

| Value | URL |
|---|---|
| (default) | `https://www.myhomeweb.com` |
| `PORTAL_STAGING` | `https://www.sta.myhomeweb.com` |
| `PORTAL_STABLE` | `https://stable-eliot-mule-balancer-2146709889.eu-west-1.elb.amazonaws.com` |
| `PORTAL_UNSTABLE` | `https://unstable-eliot-mule-balancer-1963338202.eu-west-1.elb.amazonaws.com` |

### Headers

| Header | When |
|---|---|
| `prj_name: C3X` | always |
| `auth_token: <token>` | always, unless marked "**no token**" |
| `Content-Type: application/json` | only where indicated |
| `Accept-Language: <Locale.getDefault()>` | account creation, profile update, push registration (e.g. `fr_FR`) |

### Bodies and responses

- **Request**: POST and PUT send the raw JSON, as bytes (`C4019b.java:172,197`). For GET, any
  parameters are appended to the URL and encoded (`C4019b.java:202-210`).
- **Response**: the client keeps the HTTP status code, **all headers** (read via `Response.m12091d()`)
  and the body as a string (`payload`).
  - Exception: for the `TLS` type (signcert), the binary body is Base64-encoded by the
    client itself (`C4019b.java:86`).
- **Token rotation**: if the response carries an `auth_token` header, it replaces the stored token.
  This is the case for `SignResponse`, `UserResponse`, `C3xSipUserResponse` and `C3xTLSResponse`.
  A client must therefore always take a returned `auth_token` into account.
- **Logs**: the fields `pwd`, `PswOpen`, `SipPassword` and `Password` are masked in the logs
  (`C4019b.java:155`). These are therefore the sensitive fields.

### Resource types (`p259j0/EnumC3895q.java`)

`Sign, User, Plant, Gw, Invitation, UserInvitation, UserInvitations, PushNotification,
EmailNotification, OpenMsg, SipUser, TLS`. `OpenMsg` and `UserInvitation` are never sent.

---

## 2. Session — `p259j0/C3896r.java`

| Method | Path | Headers | Body | Response |
|---|---|---|---|---|
| POST | `/eliot/users/sign_in` | Content-Type, **no token** | `{"username","pwd","appVersion","registrationId"?}` | token in the `auth_token` **header** |
| POST | `/eliot/users/sign_out` | auth_token, Content-Type | none | – |

Login details are in [`authentication.md` §2](authentication.md). After login, the app
calls sign_out via `VctLogoutController` (`p277l2/C4026E.java:107`).

---

## 3. Installations ("plants") — `p259j0/C3892n.java`

The model is `p259j0/C3891m` and the response wrapper is `PlantsResponse`.

### 3.1 `GET /eliot/plants` — list installations

Response: a JSON array (`PlantsResponse.m12066l`, parsed by `C3891m.m18718a`).

```json
[
  {
    "PlantId": "100001",
    "PlantName": "Home",
    "GatewayId": "…",
    "Address": "…", "City": "…", "CAP": "75000", "Province": "…", "Country": "FR",
    "Lat": 48.85, "Lng": 2.35,
    "Enabled": 1
  }
]
```

| Key | Type read | Note |
|---|---|---|
| `PlantId` | string containing a number | converted to `Long` (`C5022h:173`) |
| `PlantName`, `GatewayId`, `Address`, `City`, `CAP`, `Province`, `Country` | string | |
| `Lat`, `Lng` | double, then float | |
| `Enabled` | int | `> 0` means the installation is active |

### 3.2 Other routes

| Method | Path | Body / response | Caller |
|---|---|---|---|
| GET | `/eliot/plants/{plantId}` | a `C3891m` object; login checks `Enabled` (`C5022h:318`) | `C4056y:43` |
| POST | `/eliot/plants` | Content-Type. Body `{"PlantName","Address","City","Province","CAP","Country","Lat","Lng"}`. Response `{"PlantId": "…"}` | `C4038g:45` |
| GET | `/eliot/plants/{plantId}/users` | array of users (format of §11.1) | `C4052u:43` |
| DELETE | `/eliot/plants/{plantId}/users/{userId}` | `userId` = the user's `id` | `C4044m:42` |
| GET | `/eliot/plants/{plantId}/gateway/{gatewayId}/conf` | **configuration zip**, see §4 | `C4051t:176` |

---

## 4. Installation configuration (zip) — source of the LAN IP

```http
GET /eliot/plants/{plantId}/gateway/{gatewayId}/conf HTTP/1.1
Host: www.myhomeweb.com
prj_name: C3X
auth_token: <token>
```

### 4.1 Response handling (`PlantsResponse.m12068n`, l. 57-100)

1. Read the `X-AttachmentName` and `X-AttachmentMD5` headers.
2. Compute the **MD5 of the `payload` string** (the Base64 text) and compare it to `X-AttachmentMD5`,
   case-insensitively.
3. Base64-decode the payload to obtain a zip file.
4. Extract it with zip4j and the **password hardcoded in the app** (constant in
   `p074K2/C0594h.java:21` of the decompiled APK; not reproduced here). The password is only
   applied if the zip is encrypted.

The app saves the zip to `files/mhpg.zip` and writes a debug copy to
`/sdcard/c300x-config.zip`.

### 4.2 Contents (`C4051t.java:93-112`)

| Entry | Required | Contents |
|---|---|---|
| `conf.xml` | yes | video configuration (XPath below) |
| `layout.xml` | yes | not analysed |
| `archive.xml` | yes | not analysed |
| `manifest.xml` | no | not analysed |
| `read-only-par.txt` | no | JSON containing **the gateway IP** |
| `current_modality` | no | three lines `N`, `P`, `M`, joined as `"N/P/M"` |

**`read-only-par.txt`** (`p385y2/C5025k.java:309-333`):

```json
{ "wifi": { "ip": "192.168.1.42" } }
```

The IP is stored in the `gateway_ip` column of the local database, then used by network
discovery in "Triangulation" mode (§8).

**`conf.xml`** (`p074K2/C0596j.java:46-240`):

| XPath | Default |
|---|---|
| `/configuratore/setup/vdes/communication/p_default/dev` | `2` |
| `/configuratore/setup/vdes/communication/p_default/address` | P |
| `/configuratore/setup/vdes/communication/address` | N |

These values give the SCS addresses of the indoor unit and of the default door station. They are
used in the door-opening frames (see `device_actions.md`).

### 4.3 Python example

```python
import base64, hashlib, io, json, os, requests, pyzipper  # pyzipper handles ZipCrypto/AES encryption

def fetch_conf(base, token, plant_id, gw_id):
    r = requests.get(f"{base}/eliot/plants/{plant_id}/gateway/{gw_id}/conf",
                     headers={"prj_name": "C3X", "auth_token": token}, timeout=30)
    r.raise_for_status()
    payload = r.text
    md5 = r.headers.get("X-AttachmentMD5", "")
    if md5 and hashlib.md5(payload.encode()).hexdigest().lower() != md5.lower():
        raise ValueError("MD5 mismatch")
    zf = pyzipper.AESZipFile(io.BytesIO(base64.b64decode(payload)))
    zf.setpassword(os.environ["C300X_CONF_ZIP_PASSWORD"].encode())  # see §4.1
    files = {n: zf.read(n) for n in zf.namelist()}
    ip = None
    if "read-only-par.txt" in files:
        ip = json.loads(files["read-only-par.txt"])["wifi"]["ip"]
    return ip, files
```

The MD5 is computed over the Base64 text, not over the decoded bytes. This code has not yet been
tested against a real response.

---

## 5. Gateways — `p259j0/C3890l.java`

The model is `p259j0/C3889k` and the response wrapper is `MhPlayGwResponse`.

| Method | Path | Specific headers | Body / response |
|---|---|---|---|
| GET | `/eliot/plants/{plantId}/gateway/` (with trailing slash) | – | `[{"GatewayId": "…"}]`. Login only keeps the first one (`C5022h:341-345`) |
| GET | `/eliot/plants/{plantId}/gateway/{gatewayId}` | `need_token: 0`, or `need_token: 1` with `mac_address: <MAC>` | gateway object (below) |
| POST | `/eliot/plants/{plantId}/gateway` | Content-Type | `{"MacAddress": "<gateway MAC>"}`, response in the same format |

Gateway object (`C3889k.m18706a`, l. 62-76):

```json
{
  "GatewayId": "…",
  "MacAddress": "00:03:50:xx:xx:xx",
  "PswOpen": "…",          // the gateway's OpenWebNet password
  "Token": "…",
  "TemporaryToken": "…",
  "Associato": 1           // > 0: already associated
}
```

| `need_token` value | Usage |
|---|---|
| `0` | login and full alignment (`C4023B.java:46`) |
| `1` (with `mac_address`) | gateway replacement or association (`C4028G.java:49`) |

---

## 6. SIP accounts and TLS certificate

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/eliot/sip/users/plants/{plantId}/gateway/{gatewayId}` | – | array `[{SipAccount, DeviceName, GatewayId, IdDevice, Username, SipPassword}]`. 404 if there is no account |
| POST | `/eliot/sip/user` | `{"SipAccount","DeviceName","GatewayId","IdDevice"}` | not read by the app |
| POST | `/eliot/users/cert/signcert` | `{"CommonName": "<SipAccount>", "CSR": "<PEM>"}` | unencrypted zip containing `<SipAccount>.cert.pem` and `ca-chain.cert.pem` (`X-Attachment*` headers) |

All these requests carry `auth_token` and `Content-Type`.

Sequence (`p277l2/C4027F.java:97-233`):

1. GET the list. If a 404 is received, or if our account is missing: POST `/eliot/sip/user`,
   then GET again.
2. If no certificate is stored: POST signcert.

SIP servers (`server_remote` preference, always with `;transport=tls`):

| Value | Host |
|---|---|
| (default) | `app.sipserver.bs.iotleg.com` |
| `FLEXI_STAGING` | `app.sipstaging.bs.iotleg.com` |
| `FLEXI_CLOUD` | `bs-c3x-sip4-qa.westeurope.cloudapp.azure.com` |

The SIP domain is `<GatewayId>.bs.iotleg.com` (`VctLinphoneService.java:612,636`). The indoor
unit appears there as `c300x@<GatewayId>.bs.iotleg.com`. See `device_actions.md` for calls and
video.

---

## 7. Notifications

### 7.1 FCM push — `p259j0/C3894p.java`

| Method | Path | Headers |
|---|---|---|
| GET | `/eliot/plants/{plantId}/pushNotifications` | auth_token |
| POST | `/eliot/plants/{plantId}/pushNotifications/2` | auth_token, Accept-Language, Content-Type |
| DELETE | `/eliot/plants/{plantId}/pushNotifications/{PushNotifId}` | auth_token |

The `2` segment is a constant (`C4048q.java:158`). It probably designates the platform or the
application type, but this is a guess.

```json
// POST
{ "RegistrationId": "<FCM token>", "DeviceName": "<phone Wi-Fi MAC or UUID>" }
// GET → array of
{ "RegistrationId": "…", "DeviceName": "…", "PushNotifId": "…", "ResourceId": 42, "NotificationType": ["…"] }
```

App algorithm (`C4048q.java:126-232`):

1. Do a GET.
2. Look for the entry whose `DeviceName` matches our device.
3. If that entry is up to date, do nothing. Otherwise, DELETE the old entry, then POST.
4. Remember the returned `ResourceId`.

On logout, the app does a DELETE (`C4049r.java:70`).

### 7.2 E-mail — `p259j0/C3886h.java` (disabled in v1.17.5)

| Method | Path | Body |
|---|---|---|
| GET | `/eliot/plants/{plantId}/emailNotifications` | – |
| POST | `/eliot/plants/{plantId}/emailNotifications` | `{"email": "…"}` (lowercase key) |
| DELETE | `/eliot/plants/{plantId}/emailNotifications/{id}` | – |

Response items have the form `{email, ResourceId, idProgr, NotificationType[]}`.

---

## 8. LAN discovery and TCP 20000 probe — `p060I2/C0475a.java` (MhpNetworkSelector)

### 8.1 Mode selection (`m1573m`, l. 201-220)

```
Fixed IP entered by the user?                 → FixedIp      (a single IP probed)
else IP known from the config zip (§4)?       → Triangulation (a single IP; on failure → BruteForce)
else                                          → UPnP → disabled ("UPnP temporary disabled") → BruteForce
```

- **BruteForce** (`p377x2/C4935d`): scan of the Wi-Fi interface's subnet.
  - The host part is limited to 9 bits, i.e. about 512 addresses.
  - Up to 20 probes run in parallel.
  - A /32 mask is rejected.
- **FixedIp and Triangulation** (`p377x2/C4936e`): 5 attempts, 1 s apart.
- The MAC found must match that of the expected gateway. If two gateways respond,
  the app returns the `MultipleGateways` error.
- At the end of the search, the app sends the Android broadcast
  `com.legrandgroup.c300x.network.MONITOR_STATE_CHANGED`.

### 8.2 Probe protocol (`p377x2/AbstractC4933b.java:140-290`)

TCP connection to `<ip>:20000` (timeout 2000 ms, 2 attempts). Frames are ASCII and end
with `##`.

```
GW  → *#*1##                         welcome ACK
App → *99*0##                        command session
      ── authentication: OPEN (*#<nonce>##) or HMAC (*98*2##), see authentication.md §5 ──
GW  → *#*1##
App → *#1013**1##                    model identification
GW  → *#1013*<f1>*<f2>*68*…##        field 4 must be 68 (= C300X), otherwise "Not a C300X"
GW  → *#*1##
App → *#13**12##                     MAC address
GW  → *#13**12*<b1>*<b2>*<b3>*<b4>*<b5>*<b6>##   bytes in decimal (fields 4 to 9)
```

> The "field" numbering comes from the code's `split("*")`: in `*#1013*a*b*68`, element
> `[0]` is empty, `[1]` is `#1013`, and so on.

### 8.3 Python example (minimal probe)

```python
import socket

def own_recv(sock) -> str:
    buf = b""
    while not buf.endswith(b"##"):
        c = sock.recv(1)
        if not c:
            raise ConnectionError("closed")
        buf += c
    return buf.decode()

def probe(ip: str, password: str = "12345"):
    s = socket.create_connection((ip, 20000), timeout=2)
    assert own_recv(s) == "*#*1##"
    s.sendall(b"*99*0##")
    challenge = own_recv(s)
    if challenge == "*98*2##":
        raise NotImplementedError("HMAC: see authentication.md §5.5")
    nonce = challenge[2:-2]
    s.sendall(f"*#{own_calc_pass(password, nonce)}##".encode())   # authentication.md §5.4
    assert own_recv(s) == "*#*1##"
    s.sendall(b"*#1013**1##")
    model = own_recv(s).rstrip("#").split("*")
    is_c300x = len(model) > 4 and model[4] == "68"
    own_recv(s)                                   # *#*1##
    s.sendall(b"*#13**12##")
    f = own_recv(s).rstrip("#").split("*")
    mac = ":".join(f"{int(x):02X}" for x in f[4:10])
    s.close()
    return is_c300x, mac
```

---

## 9. Local OpenWebNet channels (TCP 20000)

| Channel (`p046G2/C0279f`) | Protocol | Opening | Use in v1.17.5 |
|---|---|---|---|
| `Operativing` | text OpenWebNet (`p243h0/C3792k`) | `*99*0##` (CMD) or `*99*1##` (MON) | **never opened** outside `p046G2` (code present but unused) |
| `Managing` | XOpen, OpenWebNet wrapped in XML (`p243h0/C3800s`) | `OWNSetProtocol` | association (`GWASSOC`, `DEVREP` in `C4036e`) and release notes (`GETRELNOTES` in `C4934c`) |

In raw OpenWebNet, the app only sends the discovery probe (§8) and the firmware status read
`*#130**1##` (`p377x2/C4932a.java:226`). **No action** (door, light, camera) goes through
TCP 20000. All actions go through SIP (`device_actions.md`).

### 9.1 Control frames (`p243h0/AbstractC3790i.java`)

| Frame | Meaning |
|---|---|
| `*#*1##` | ACK |
| `*#*0##` | NACK |
| `*99*0##` | open a **command** session |
| `*99*1##` | open a **monitor** session (events pushed continuously) |

### 9.2 Frame formats (`p243h0/C3793l.java:13-22`)

| Form | Format | Example |
|---|---|---|
| Command | `*WHO*WHAT*WHERE##` | `*8*19*20##` |
| Status request | `*#WHO*WHERE##` | `*#13**12##` |
| Dimension request | `*#WHO*WHERE*DIM##` | – |
| Dimension write | `*#WHO*WHERE*#DIM*V1*V2…##` | – |

### 9.3 Command session behaviour (`C3792k.java`)

- After each command, the app waits up to 30 s for `*#*1##` or `*#*0##`
  (`AbstractC3790i.f18738e`).
- The CMD session **expires after 30 s**. Beyond 29 s since the last frame, the app closes
  the session and opens a new one (`C3792k.java:219-229`). On the Home Assistant side, open one
  session per command, or handle reconnection.
- The MON session reads continuously in a dedicated thread (`frameReadLoop`) and passes each frame
  to the listeners.

---

## 10. Firebase / FCM

| Key (`res/values/strings.xml`) | Value |
|---|---|
| `project_id` | `door-eliot` |
| `gcm_defaultSenderId` | `615467758582` |
| `google_app_id` | `1:615467758582:android:4b6cdc7c785d68c8` |
| `firebase_database_url` | `https://door-eliot.firebaseio.com` (declared but unused) |
| `google_api_key` | public Android client key (l. 1068), useless for Home Assistant |

- **SIP REGISTER in remote mode**: Linphone adds to the contact
  `pn-param=door-eliot;pn-provider=fcm;pn-timeout=0;pn-prid=<FCM token>`
  (`VctLinphoneService.java:988`). The SIP server thus wakes the app via push when a call comes in.
- **Data messages** received (`VctMessagingService.java`): keys `message`, `loc-args`, `id_gateway`,
  `id_message` and `id_device`.
  - If `loc-args` contains `c300x@<GatewayId>.bs.iotleg.com`, it is an **incoming call**: the app
    refreshes SIP.
  - `id_message` values:

    | Value | Effect |
    |---|---|
    | `PLANTCHG` | full realignment |
    | `FWAVAIL` | new firmware available |
    | `NEWASWMMSG` | new answering-machine message |
    | `UNTIEUSER` | forced logout |
    | `UNTIEUSIP` | forced logout if `id_device` matches our device |

For Home Assistant, receiving these pushes would require registering an FCM client with the
`door-eliot` project, which is fragile. It is better to detect calls via the OpenWebNet MON channel
or via a registered SIP client (see `device_actions.md`).

---

## 11. User and invitations

### 11.1 User — `p259j0/C3899u.java` (model `C3897s`)

| Method | Path | Headers | Body |
|---|---|---|---|
| GET | `/eliot/user` | auth_token | – |
| POST | `/eliot/user` (creation) | **no token**, Accept-Language, Content-Type | full profile |
| PUT | `/eliot/user` | auth_token, Content-Type, and Accept-Language for the profile | `{"Password"}` or profile or consents |
| DELETE | `/eliot/user` | auth_token | – |
| POST | `/eliot/user/pwd-reset` | **no token**, Content-Type | `{"Email": "…"}` |

`GET /eliot/user` response:

```json
{
  "id": 1234, "username": "…", "Email": "…",
  "Name": "…", "Surname": "…",
  "Address": "…", "CAP": "…", "City": "…", "Province": "…", "Country": "…",
  "Lat": 0.0, "Lng": 0.0,
  "Gender": "…", "Telephone": "…", "Note": "…", "People": 0,
  "Agreement": 1, "ContractRev": 3, "PrivacyMarketing": 0, "GoogleAnalytics": 0
}
```

`Agreement` is wrongly read via `optInt("Address")` (`C3897s.java:264`). This is an app bug with
no consequence for Home Assistant.

If the contract version (`ContractRev`) is outdated, error 992 `e_UpdateTermsAndConditions`
appears. The current version can be read from the
`<meta name="Contract_Version" content="N">` tag of
`<base>/eliot/legacy/c3x_gdpr_contract_agreement_<locale>`.

### 11.2 Invitations — `p259j0/C3887i.java`

| Method | Path | Body / response |
|---|---|---|
| GET | `/eliot/invitations` | `[{"InvitationId": 1, "PlantId": 2, "PlantName": "…"}]` |
| POST | `/eliot/plants/{plantId}/invitations` | `{"Email": "…"}` |
| PUT | `/eliot/plants/{plantId}/invitations/{invitationId}` | accepts the invitation |
| DELETE | `/eliot/plants/{plantId}/invitations/{invitationId}` | declines the invitation |

---

## 12. App sequences

### Login (`p385y2/C5022h.java`)

```
POST /eliot/users/sign_in
GET  /eliot/plants
GET  /eliot/invitations
GET  /eliot/plants
for each Enabled plant:
    GET /eliot/plants/{id}
    GET /eliot/plants/{id}/gateway/            → first GatewayId
    GET /eliot/plants/{id}/gateway/{gw}        (need_token: 0) → PswOpen
```

### Installation alignment (`p385y2/C5025k.java`)

```
GET  …/gateway/{gw}/conf          → zip → read-only-par.txt → IP
GET/POST SIP users + signcert     → SIP credentials and certificate
GET/DELETE/POST pushNotifications
(email: skipped)
GET  /eliot/user
→ network discovery starts (Triangulation on the IP obtained)
```

### Associating a new gateway (`p385y2/C5017c.java:160-369`)

1. Login.
2. LAN discovery (§8) to obtain the gateway's MAC.
3. `POST /eliot/plants`.
4. `POST /eliot/plants/{id}/gateway {"MacAddress"}`, or alternatively GET the gateway with
   `need_token: 1`.
5. If `Associato <= 0`, the OPEN password becomes `TemporaryToken` (or `Token` if
   `TemporaryToken` is empty). Otherwise, the app returns the `e_GatewayAlreadyAssociated` error.
6. Wait for local commissioning.
7. Create the SIP accounts.
8. Wait for the gateway on port 20000.

### Minimal sequence for Home Assistant

```
sign_in → GET plants → GET gateway/ → GET gateway/{gw} (PswOpen)
        → GET conf (IP + dev/addr addresses)
        → GET sip/users (SipAccount + SipPassword) [+ signcert if needed]
        → SIP/TLS REGISTER (local ;maddr=<IP> or remote app.sipserver.bs.iotleg.com)
        → SIP MESSAGE "*8*19*<where>##" then "*8*20*<where>##" → sip:c300x@<GatewayId>.bs.iotleg.com
```

Alternative path **not used by the app**, to be tested: send the same frames over an OpenWebNet
command session (TCP 20000, authenticated with `PswOpen`). This would be simpler than SIP, but
nothing in the APK guarantees that the gateway accepts `*8*19*…` over this channel.

---

## 13. Result codes (`p243h0/C3785d.java:35-89`)

HTTP and business codes returned by Eliot:

| Code | Name | Code | Name |
|---|---|---|---|
| 200 | NoError | 435 | EmailAlreadyUsed |
| 209 | passwordExpired | 436 | UserAlreadyAssociated |
| 210 | contractExpired | 437 | SipUserAlreadyPresent |
| 211 | userComingFromMyHomePlay | 441 | TokenNotValidated |
| 400 | BadRequest | 450 | GatewayAlreadyAssociated2 |
| 401 | Unauthorized (redo sign_in) | 460 | GatewayNotPresented |
| 404 | NotFound | 461 | MaxSipUsers |
| 432 | InvalidPassword | 500 | InternalServerError |
| 433 | PasswordAlreadyUsed | 503 | ServiceUnavailable |
| 434 | UserDisabled | | |

App-internal codes, never returned by the server:

| Code | Name |
|---|---|
| 1002–1014 | OpenWebNet socket or frame errors (NotConnected, NotInRange, UnableSendFrame, MonitorDown…) |
| 2001 | SessionExpired |
| 2002 | NACK |
| 3015–3025 | local errors (checksum, XML, NoPlants, MultipleGateways, Gcm…) |
| 992–999 | local errors (terms and conditions, SSL, network…) |

---

## 14. Reliability of the information

**Certain**, because read directly in the code:

- all methods, paths, headers, body keys and parsed response keys above;
- the absence of an opening endpoint;
- the IP being available only via `read-only-par.txt`;
- the zip password (constant in `p074K2/C0594h.java:21`);
- the absence of UDP discovery;
- e-mail notifications being disabled.

**Inferred**:

- the meaning of the `2` in `pushNotifications/2`;
- the absence of `PlantId` in `GET /eliot/plants/{id}`;
- the contract version URL.

**Unknown**: the format of the responses to PUT, DELETE and `POST /eliot/sip/user`, which the app
does not read. The `layout.xml`, `archive.xml` and `manifest.xml` schemas have not been analysed.

**To be validated on a real installation**:

- the zip Python example;
- the exact format of `*#1013**1##`;
- the actual lifetime of the `auth_token`.
