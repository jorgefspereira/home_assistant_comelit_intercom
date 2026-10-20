"""Constants for the Comelit ViP integration."""

DOMAIN = "comelit_intercom"
BASE_URL = "https://api.comelitgroup.com"
CLIENT_ID = "kgDV0WRlQcSF4jPsz887lOTPyVVtP7Oh"
REDIRECT_URI = "https://app.comelitgroup.com/oauth_redirect/comelit"
ICE_SERVERS = (
    "stun:stun1.l.google.com:19302",
    "stun:stun2.l.google.com:19302",
    "stun:stun.cloud.comelitgroup.com:3478",
)
CONF_RESOLUTION = "resolution"
DEFAULT_RESOLUTION = "HD"
IDLE_TIMEOUT = 10
STARTUP_TIMEOUT = 60
FRAME_TIMEOUT = 10
RETRY_DELAY = 30
