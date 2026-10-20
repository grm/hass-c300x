# BTicino Classe 300X for Home Assistant

**Local** Home Assistant integration for the BTicino Classe 300X (C300X) video door entry system, installable through [HACS](https://hacs.xyz).

The BTicino cloud is only used once, during setup: it provides the gateway's OpenWebNet password and signs a SIP certificate. After that, everything stays on your local network between Home Assistant and the gateway (OpenWebNet on port 20000, SIP over TLS on port 5061, SRTP for audio and video).

## Features

| Entity | Description |
|---|---|
| `event` Doorbell | Fired when someone rings (`address` attribute = door station) |
| `event` Door | Fired when the door is opened (app, indoor unit or Home Assistant) |
| `binary_sensor` Ringing | On while the door station is ringing |
| `binary_sensor` Call in progress | Diagnostic: Home Assistant is in a call with the door station |
| `button` Open door | One entity per configured lock |
| `button` Stair light | Only if an address is configured (untested) |
| `button` Refresh image | Calls the door station for a few seconds to update the image |
| `camera` | Live video (MJPEG, 400×288, 10 fps) when opened; last image otherwise |
| `media_player` Speaker | Plays a file, a stream or a TTS message on the door station speaker |

Example: play a message when someone rings.

```yaml
automation:
  - alias: Message at the door
    triggers:
      - trigger: state
        entity_id: event.home_doorbell
    actions:
      - action: tts.speak
        target:
          entity_id: tts.google_translate_en_com
        data:
          media_player_entity_id: media_player.home_speaker
          message: "Hello, I'll be right down."
```

> The door station rejects calls while it is ringing (about 30 s). The message is therefore played once the ringing is over, or after someone has answered and hung up. If the door station is busy, the action fails with an explicit error.

## Installation

1. HACS → ⋮ menu → *Custom repositories* → `https://github.com/grm/hass-c300x`, category *Integration*.
2. Install **BTicino Classe 300X**, then restart Home Assistant.
3. *Settings → Devices & services → Add integration → BTicino Classe 300X*.
4. Sign in with your **Door Entry** app account, pick the installation, then check the gateway's local IP address (give it a fixed lease in your router or DHCP server).

The integration reuses the installation's existing SIP account (the app's one) with a new private key. It only creates one if there is none.

### Certificate renewal

The SIP certificate signed by the cloud is valid for about 2 years.

- If you tick **Remember the password** (at setup or when signing in again), the integration renews the certificate automatically 30 days before it expires. It also fetches the new gateway password if the gateway has been reset. The password is stored in Home Assistant's configuration storage, like other cloud integrations.
- Otherwise, a warning appears in *Settings → Repairs* 30 days before expiry, with a button to sign in again.

### Requirements

- Home Assistant 2025.2 or newer, with `ffmpeg` (already included in Home Assistant OS and the official Docker image).
- **libspeex**, only for the speaker: the door station only accepts the Speex codec. Without it, everything else works and the speaker returns a clear error. On Debian or Ubuntu: `apt install libspeex1`. On macOS: `brew install speex`.

## Options

- **Door station address** (`20` by default): door station called for video and audio.
- **Lock addresses** (`20` by default): comma-separated list, one button per lock.
- **Stair light address**: empty by default.
- **Speaker gain** (18 dB by default): compression and amplification of the messages played.

## Limitations

- **No video while ringing**: the gateway answers "busy" (486) as long as the door station is ringing.
- The camera image is **not** refreshed in the background. Each refresh is a call that turns on the door station's screen and light. Use the *Refresh image* button or open the live view.
- **One-way** audio (Home Assistant → door station). There is no two-way intercom from Home Assistant, because the door station is half-duplex.
- Calls are limited to 3 minutes.
- Not field-tested: the stair light and SIP account creation (for installations that have none).

## Development

```bash
pip install -r requirements_test.txt
ruff check custom_components tests
pytest
```

## Troubleshooting

Enable debug logs:

```yaml
logger:
  logs:
    custom_components.doorentry: debug
```

The integration diagnostics (*Download diagnostics*) redact the email, the passwords, the SIP account and the certificates.

## Technical documentation

The [`docs/`](docs/) folder describes the protocol (Eliot cloud API, OpenWebNet authentication, SIP commands, field tests). The [`tools/`](tools/) folder contains the standalone field-test scripts used to validate the protocol outside Home Assistant (see [`tools/README.md`](tools/README.md)).

## Disclaimer

Independent project, not affiliated with BTicino or Legrand. Use at your own risk.
