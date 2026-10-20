"""Device identity shared by the camera and release-button entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import ComelitConfigEntry
from .api import Endpoint
from .const import DOMAIN


class ComelitEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ComelitConfigEntry, endpoint: Endpoint):
        self.entry = entry
        self.endpoint = endpoint
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, endpoint.id)},
            name=endpoint.name,
            manufacturer="Comelit",
            model="ViP endpoint",
        )

    def start_reauth(self):
        self.entry.async_start_reauth(self.hass)
