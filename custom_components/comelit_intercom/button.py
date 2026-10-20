"""Momentary commands: the API provides no reliable locked/unlocked state."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import ComelitAuthError, ComelitError
from .entity import ComelitEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        ComelitReleaseButton(entry, endpoint)
        for endpoint in entry.runtime_data.endpoints
        if endpoint.power
    )


class ComelitReleaseButton(ComelitEntity, ButtonEntity):
    _attr_name = "Activate"
    _attr_translation_key = "activate"
    _attr_icon = "mdi:door-open"

    def __init__(self, entry, endpoint):
        super().__init__(entry, endpoint)
        self._attr_unique_id = f"{entry.unique_id}_{endpoint.id}_activate"

    async def async_press(self) -> None:
        try:
            await self.entry.runtime_data.client.release(self.endpoint)
        except ComelitAuthError as err:
            self.start_reauth()
            raise HomeAssistantError(str(err)) from err
        except ComelitError as err:
            raise HomeAssistantError(
                f"{err}. The release was not confirmed; its physical outcome may be unknown."
            ) from err
