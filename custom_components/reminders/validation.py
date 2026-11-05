"""Use the same schema implementation as the running Home Assistant."""

from importlib import import_module

from homeassistant.helpers import config_validation as cv

# Probatio is also installed by HA releases that still use Voluptuous. Match
# HA's schemas rather than choosing whichever library happens to import first.
Schema = type(cv.PLATFORM_SCHEMA)
vol = import_module(Schema.__module__.split(".", 1)[0])
