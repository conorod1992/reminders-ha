"""Constants for Reminders."""

from typing import Final

DOMAIN: Final = "reminders"
STORAGE_KEY: Final = f"{DOMAIN}.storage"
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 6

MOBILE_ACTION_EVENT: Final = "mobile_app_notification_action"
MOBILE_ACTION_PREFIX: Final = "REMINDERS_"
LIFECYCLE_EVENT: Final = f"{DOMAIN}_lifecycle"
SAVE_DELAY: Final = 2.0

SERVICE_CREATE: Final = "create"
SERVICE_CREATE_RECURRING: Final = "create_recurring"
SERVICE_CREATE_TRIGGERED: Final = "create_triggered"
SERVICE_FIRE_TRIGGER: Final = "fire_trigger"
SERVICE_ACKNOWLEDGE: Final = "acknowledge"
SERVICE_DELETE: Final = "delete"
SERVICE_GET: Final = "get"
SERVICE_LIST: Final = "list"
SERVICE_SET_USER_PREFERENCES: Final = "set_user_preferences"
SERVICE_SNOOZE: Final = "snooze"
SERVICE_TEST_DELIVERY: Final = "test_delivery"
SERVICE_UPDATE: Final = "update"

CHANNEL_PERSISTENT_NOTIFICATION: Final = "persistent_notification"
CHANNEL_PHONE: Final = "phone"
CHANNEL_VOICE: Final = "voice"
SUPPORTED_CHANNELS: Final = {
    CHANNEL_PERSISTENT_NOTIFICATION,
    CHANNEL_PHONE,
    CHANNEL_VOICE,
}
