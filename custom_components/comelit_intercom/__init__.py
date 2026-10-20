"""Home Assistant support for Comelit cloud-signalled ViP intercoms."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import probatio as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.importlib import async_import_module

from .api import ComelitAuthError, ComelitClient, ComelitError, Endpoint
from .const import CONF_RESOLUTION, DEFAULT_RESOLUTION, DOMAIN

if TYPE_CHECKING:
    from .media import VideoManager

PLATFORMS = [Platform.CAMERA, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class ComelitData:
    """Data owned by one config entry."""

    client: ComelitClient
    endpoints: list[Endpoint]
    videos: dict[str, VideoManager] = field(default_factory=dict)

    async def close(self) -> None:
        await asyncio.gather(*(video.close() for video in self.videos.values()))


type ComelitConfigEntry = ConfigEntry[ComelitData]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Keep the action registered even when an account is temporarily unavailable."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        "record",
        entity_domain=Platform.CAMERA,
        schema={
            vol.Required("filename"): cv.string,
            vol.Optional("duration", default=20): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        },
        func="async_record",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ComelitConfigEntry) -> bool:
    client = ComelitClient(
        async_get_clientsession(hass), entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
    )
    try:
        endpoints = await client.discover()
    except ComelitAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ComelitError as err:
        raise ConfigEntryNotReady(str(err)) from err
    # Entry removal and config-flow imports must work even when the video
    # dependency could not be installed. Load it only during actual setup.
    try:
        media = await async_import_module(hass, f"{__package__}.media")
    except ImportError as err:
        raise ConfigEntryNotReady(
            "The Comelit video dependency could not be loaded. Check the installation logs."
        ) from err
    resolution = entry.options.get(CONF_RESOLUTION, DEFAULT_RESOLUTION)
    entry.runtime_data = ComelitData(
        client,
        endpoints,
        {
            endpoint.id: media.VideoManager(client, endpoint.id, resolution)
            for endpoint in endpoints
            if endpoint.camera
        },
    )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await entry.runtime_data.close()
        raise

    async def stop(event):
        await entry.runtime_data.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ComelitConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ComelitConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
    return unloaded
