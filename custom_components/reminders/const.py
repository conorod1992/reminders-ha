"""Constants for Reminders."""

from typing import Final

DOMAIN: Final = "reminders"
STORAGE_KEY: Final = f"{DOMAIN}.storage"
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1
SAVE_DELAY: Final = 2.0

SERVICE_CREATE: Final = "create"
SERVICE_DELETE: Final = "delete"
SERVICE_GET: Final = "get"
SERVICE_LIST: Final = "list"
SERVICE_SET_USER_PREFERENCES: Final = "set_user_preferences"
SERVICE_SNOOZE: Final = "snooze"
SERVICE_UPDATE: Final = "update"

CHANNEL_PERSISTENT_NOTIFICATION: Final = "persistent_notification"
CHANNEL_PHONE: Final = "phone"
CHANNEL_VOICE: Final = "voice"
SUPPORTED_CHANNELS: Final = {
    CHANNEL_PERSISTENT_NOTIFICATION,
    CHANNEL_PHONE,
    CHANNEL_VOICE,
}
