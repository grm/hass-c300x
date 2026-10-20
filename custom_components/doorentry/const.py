"""Constants for the BTicino Door Entry (Classe 300X) integration."""
import datetime as dt
from typing import Final

DOMAIN: Final = "doorentry"
MANUFACTURER: Final = "BTicino"
MODEL: Final = "Classe 300X"

# Config entry data
CONF_PLANT_ID: Final = "plant_id"
CONF_GATEWAY_ID: Final = "gateway_id"
CONF_OPEN_PASSWORD: Final = "open_password"  # PswOpen, OpenWebNet password
CONF_SIP_ACCOUNT: Final = "sip_account"
CONF_CERT: Final = "certificate"
CONF_KEY: Final = "private_key"
CONF_CA: Final = "ca_chain"

# Options
CONF_DEVADDR: Final = "devaddr"
CONF_LOCKS: Final = "locks"
CONF_STAIR_LIGHT: Final = "stair_light"
CONF_TALK_GAIN: Final = "talk_gain"

# Single door station: dev=2, P=0 → DEVADDR 20, door lock *8*19*20## (verified)
DEFAULT_DEVADDR: Final = "20"
DEFAULT_LOCKS: Final = "20"
DEFAULT_TALK_GAIN: Final = 18

# The ringing sensor turns off on the end-of-call frame; this is a safety net (rings last ≈ 29 s)
RING_TIMEOUT: Final = 60

# Certificate renewal: the Eliot cloud signs certificates for about 2 years
CONF_REMEMBER: Final = "remember_password"
RENEW_BEFORE: Final = dt.timedelta(days=30)
RENEW_CHECK_INTERVAL: Final = dt.timedelta(days=1)
