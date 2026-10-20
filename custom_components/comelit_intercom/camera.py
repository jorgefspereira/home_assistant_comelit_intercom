"""Standard HA JPEG/MJPEG camera backed by the monitor's WebRTC connection."""

import logging
from pathlib import Path

import av
from homeassistant.components.camera import Camera
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .api import ComelitAuthError, ComelitError
from .entity import ComelitEntity

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        ComelitCamera(entry, endpoint)
        for endpoint in entry.runtime_data.endpoints
        if endpoint.camera
    )


class ComelitCamera(ComelitEntity, Camera):
    _attr_name = None
    _attr_brand = "Comelit"
    _attr_model = "ViP WebRTC"
    _attr_frame_interval = 0.25

    def __init__(self, entry, endpoint):
        Camera.__init__(self)
        ComelitEntity.__init__(self, entry, endpoint)
        self._attr_unique_id = f"{entry.unique_id}_{endpoint.id}_camera"
        self._attr_entity_registry_enabled_default = "CAMERA" in endpoint.categories
        self.video = entry.runtime_data.videos[endpoint.id]
        self._attr_is_recording = False
        self._image_failed = False

    async def async_camera_image(self, width=None, height=None):
        try:
            image = await self.video.image()
        except ComelitAuthError as err:
            self.start_reauth()
            self._report_image_failure(err)
            return None
        except (ComelitError, TimeoutError) as err:
            self._report_image_failure(err)
            return None
        if self._image_failed:
            _LOGGER.info("Comelit camera %s video recovered", self.endpoint.name)
            self._image_failed = False
        return image

    def _report_image_failure(self, error: Exception) -> None:
        # HA's MJPEG callback expects None when no image is available. Raising
        # here aborts an already prepared HTTP response with an aiohttp traceback.
        if not self._image_failed:
            _LOGGER.warning(
                "Comelit camera %s temporarily unavailable: %s",
                self.endpoint.name,
                str(error) or "Video request timed out",
            )
            self._image_failed = True
        else:
            _LOGGER.debug("Comelit camera %s: %s", self.endpoint.name, error)

    async def async_record(self, filename: str, duration: int = 20):
        if not Path(filename).is_absolute():
            raise ServiceValidationError(
                "The recording filename must be an absolute path"
            )
        destination = await self.hass.async_add_executor_job(Path(filename).resolve)
        if destination.suffix.lower() != ".mp4":
            raise ServiceValidationError("The recording filename must end in .mp4")
        if not await self.hass.async_add_executor_job(
            self.hass.config.is_allowed_path, str(destination)
        ):
            raise ServiceValidationError(
                "The recording directory is not in allowlist_external_dirs"
            )
        if self._attr_is_recording:
            raise HomeAssistantError("A recording is already running for this camera")
        self._attr_is_recording = True
        self.async_write_ha_state()
        try:
            await self.video.record(destination, duration)
        except ComelitAuthError as err:
            self.start_reauth()
            raise HomeAssistantError(str(err)) from err
        except (ComelitError, OSError, TimeoutError, ValueError, av.FFmpegError) as err:
            raise HomeAssistantError(
                "Comelit recording failed; existing output was preserved"
            ) from err
        finally:
            self._attr_is_recording = False
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        await self.video.close()
        await super().async_will_remove_from_hass()
