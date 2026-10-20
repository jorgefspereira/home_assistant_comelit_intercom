"""Check real Home Assistant interfaces with mocked Comelit I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from custom_components.comelit_intercom import (
    ComelitData,
    async_setup,
    async_setup_entry,
    async_unload_entry,
    config_flow,
)
from custom_components.comelit_intercom.api import (
    ComelitAuthError,
    ComelitError,
    Endpoint,
)
from custom_components.comelit_intercom.button import ComelitReleaseButton
from custom_components.comelit_intercom.camera import ComelitCamera
from custom_components.comelit_intercom.const import DOMAIN
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)


@pytest.fixture
async def hass(tmp_path, monkeypatch):
    import custom_components.comelit_intercom as integration

    monkeypatch.setattr(integration, "async_get_clientsession", Mock())
    monkeypatch.setattr(config_flow, "async_get_clientsession", Mock())
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    yield instance
    await instance.async_stop(force=True)


def entry():
    camera = Endpoint("camera#1", "Entrada", ("RTCSessionController",), ("CAMERA",))
    lock = Endpoint("lock#1", "Fechadura entrada", ("PowerController",), ())
    runtime = ComelitData(AsyncMock(), [camera, lock], {camera.id: AsyncMock()})
    return SimpleNamespace(
        unique_id="user@example.invalid",
        data={"email": "user@example.invalid", "password": "fake"},
        entry_id="test",
        runtime_data=runtime,
        options={},
        async_start_reauth=Mock(),
        async_on_unload=Mock(),
        add_update_listener=Mock(),
    )


async def test_config_flow_validates_discovery_without_actuation(hass, monkeypatch):
    fake = AsyncMock()
    fake.discover.return_value = entry().runtime_data.endpoints
    monkeypatch.setattr(config_flow, "ComelitClient", Mock(return_value=fake))
    flow = config_flow.ComelitConfigFlow()
    flow.hass, flow.context = hass, {"source": "user"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.handler = DOMAIN
    result = await flow.async_step_user(
        {"email": " User@example.invalid ", "password": "fake"}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["email"] == "user@example.invalid"
    fake.discover.assert_awaited_once()
    fake.release.assert_not_awaited()
    fake.offer.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ComelitAuthError("invalid"), "invalid_auth"),
        (ComelitError("offline"), "cannot_connect"),
        (None, "no_devices"),
    ],
)
async def test_config_flow_errors(hass, monkeypatch, failure, expected):
    fake = AsyncMock()
    fake.discover.side_effect = failure
    fake.discover.return_value = []
    monkeypatch.setattr(config_flow, "ComelitClient", Mock(return_value=fake))
    flow = config_flow.ComelitConfigFlow()
    flow.hass, flow.context, flow.handler = hass, {"source": "user"}, DOMAIN
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    result = await flow.async_step_user(
        {"email": "user@example.invalid", "password": "fake"}
    )
    assert result["errors"] == {"base": expected}


async def test_reauthentication_updates_existing_entry(hass, monkeypatch):
    account = entry()
    fake = AsyncMock()
    fake.discover.return_value = account.runtime_data.endpoints
    monkeypatch.setattr(config_flow, "ComelitClient", Mock(return_value=fake))
    flow = config_flow.ComelitConfigFlow()
    flow.hass, flow.context, flow.handler = hass, {"source": "reauth"}, DOMAIN
    flow._get_reauth_entry = Mock(return_value=account)
    flow.async_update_reload_and_abort = Mock(
        return_value={"type": "abort", "reason": "reauth_successful"}
    )
    result = await flow.async_step_reauth_confirm({"password": "new-fake"})
    assert result["reason"] == "reauth_successful"
    assert (
        flow.async_update_reload_and_abort.call_args.kwargs["data_updates"]["password"]
        == "new-fake"
    )


async def test_button_dispatch_and_auth_recovery(hass):
    account = entry()
    button = ComelitReleaseButton(account, account.runtime_data.endpoints[1])
    button.hass = hass
    await button.async_press()
    account.runtime_data.client.release.assert_awaited_once_with(button.endpoint)
    account.runtime_data.client.release.side_effect = ComelitAuthError("expired")
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    account.async_start_reauth.assert_called_once_with(hass)


async def test_camera_image_and_record_path_permissions(hass, tmp_path):
    account = entry()
    camera = ComelitCamera(account, account.runtime_data.endpoints[0])
    camera.hass = hass
    camera.async_write_ha_state = Mock()
    camera.video.image.return_value = b"jpeg"
    assert await camera.async_camera_image() == b"jpeg"
    assert camera.frame_interval == 0.25
    assert camera.entity_registry_enabled_default
    with pytest.raises(ServiceValidationError):
        await camera.async_record(str(tmp_path / "out.mp4"))
    camera.video.record.assert_not_awaited()
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await camera.async_record(str(tmp_path / "out.mp4"), 3)
    camera.video.record.assert_awaited_once_with(tmp_path / "out.mp4", 3)
    assert not camera.is_recording
    with pytest.raises(ServiceValidationError):
        await camera.async_record("relative.mp4")


async def test_non_camera_rtc_endpoints_disabled_by_default(hass):
    account = entry()
    endpoint = Endpoint("camera#1", "Switchboard", ("RTCSessionController",), ())
    assert not ComelitCamera(account, endpoint).entity_registry_enabled_default


async def test_camera_transient_failure_is_logged_once_until_recovery(hass, caplog):
    account = entry()
    camera = ComelitCamera(account, account.runtime_data.endpoints[0])
    camera.hass = hass
    camera.video.image.side_effect = ComelitError("Comelit request failed (HTTP 500)")
    assert await camera.async_camera_image() is None
    assert await camera.async_camera_image() is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "HTTP 500" in warnings[0].message
    assert not warnings[0].exc_info
    account.async_start_reauth.assert_not_called()
    camera.video.image.side_effect = None
    camera.video.image.return_value = b"jpeg"
    assert await camera.async_camera_image() == b"jpeg"
    camera.video.image.side_effect = ComelitError("No video frames arrived")
    assert await camera.async_camera_image() is None
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


async def test_camera_auth_failure_requests_reauth_without_http_traceback(hass):
    account = entry()
    camera = ComelitCamera(account, account.runtime_data.endpoints[0])
    camera.hass = hass
    camera.video.image.side_effect = ComelitAuthError("expired")
    assert await camera.async_camera_image() is None
    account.async_start_reauth.assert_called_once_with(hass)


async def test_mjpeg_stream_ends_cleanly_on_camera_failure(hass, monkeypatch):
    from homeassistant.components import camera as ha_camera

    account = entry()
    camera = ComelitCamera(account, account.runtime_data.endpoints[0])
    camera.hass = hass
    camera.video.image.side_effect = ComelitError("Comelit request failed (HTTP 500)")
    response = AsyncMock()
    monkeypatch.setattr(ha_camera.web, "StreamResponse", Mock(return_value=response))
    request = Mock()
    result = await ha_camera.async_get_still_stream(
        request, camera.async_camera_image, "image/jpeg", camera.frame_interval
    )
    assert result is response
    response.prepare.assert_awaited_once_with(request)
    response.write.assert_not_awaited()


async def test_recording_failure_remains_visible_to_caller(hass, tmp_path):
    account = entry()
    camera = ComelitCamera(account, account.runtime_data.endpoints[0])
    camera.hass = hass
    camera.async_write_ha_state = Mock()
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    camera.video.record.side_effect = ComelitError("No video frames arrived")
    with pytest.raises(HomeAssistantError, match="recording failed"):
        await camera.async_record(str(tmp_path / "out.mp4"))
    assert not camera.is_recording


async def test_record_action_registered(hass):
    assert await async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, "record")


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ComelitAuthError("invalid"), ConfigEntryAuthFailed),
        (ComelitError("offline"), ConfigEntryNotReady),
    ],
)
async def test_entry_setup_failure_classification(hass, monkeypatch, failure, expected):
    import custom_components.comelit_intercom as integration

    fake = AsyncMock()
    fake.discover.side_effect = failure
    monkeypatch.setattr(integration, "ComelitClient", Mock(return_value=fake))
    with pytest.raises(expected):
        await async_setup_entry(hass, entry())


async def test_unload_closes_media_only_if_platforms_unloaded(hass, monkeypatch):
    account = entry()
    unload = AsyncMock(return_value=False)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    assert not await async_unload_entry(hass, account)
    video = next(iter(account.runtime_data.videos.values()))
    video.close.assert_not_awaited()
    unload.return_value = True
    assert await async_unload_entry(hass, account)
    video.close.assert_awaited_once()
