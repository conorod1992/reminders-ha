"""Constants for Reminders."""

from typing import Final

DOMAIN: Final = "reminders"
STORAGE_KEY: Final = f"{DOMAIN}.storage"
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 8

MOBILE_ACTION_EVENT: Final = "mobile_app_notification_action"
MOBILE_ACTION_PREFIX: Final = "REMINDERS_"
LIFECYCLE_EVENT: Final = f"{DOMAIN}_lifecycle"
LIFECYCLE_SCHEMA_VERSION: Final = 1
SAVE_DELAY: Final = 2.0

SERVICE_CREATE: Final = "create"
SERVICE_CREATE_RECURRING: Final = "create_recurring"
SERVICE_CREATE_TRIGGERED: Final = "create_triggered"
SERVICE_FIRE_TRIGGER: Final = "fire_trigger"
SERVICE_ACKNOWLEDGE: Final = "acknowledge"
SERVICE_COMPLETE: Final = "complete"
SERVICE_EXTERNAL_ACTION: Final = "external_action"
SERVICE_DELETE: Final = "delete"
SERVICE_GET: Final = "get"
SERVICE_LIST: Final = "list"
SERVICE_PAUSE: Final = "pause"
SERVICE_RECONCILE_SOURCE: Final = "reconcile_source"
SERVICE_RESUME: Final = "resume"
SERVICE_SET_USER_PREFERENCES: Final = "set_user_preferences"
SERVICE_SKIP_NEXT: Final = "skip_next"
SERVICE_SNOOZE: Final = "snooze"
SERVICE_TEST_DELIVERY: Final = "test_delivery"
SERVICE_UPDATE: Final = "update"
SERVICE_UPSERT: Final = "upsert"

CHANNEL_PERSISTENT_NOTIFICATION: Final = "persistent_notification"
CHANNEL_PHONE: Final = "phone"
CHANNEL_VOICE: Final = "voice"
SUPPORTED_CHANNELS: Final = {
    CHANNEL_PERSISTENT_NOTIFICATION,
    CHANNEL_PHONE,
    CHANNEL_VOICE,
}
