"""Video lifecycle, codec and output preservation checks, all offline."""

import asyncio
from fractions import Fraction
from unittest.mock import AsyncMock

import av
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, VideoStreamTrack
from custom_components.comelit_intercom import media
from custom_components.comelit_intercom.api import ComelitAuthError, ComelitError


def frame(number=0):
    result = av.VideoFrame(width=64, height=48, format="yuv420p")
    for plane in result.planes:
        plane.update(bytes(plane.buffer_size))
    result.pts = number * 3600
    result.time_base = Fraction(1, 90000)
    return result


async def test_offer_preserves_real_sha256_certificate():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("video", direction="recvonly")
    pc.createDataChannel("libcomelit-channel")
    try:
        offer = await pc.createOffer()
        result = media.compatible_offer(offer.sdp)
        original = offer.sdp.splitlines()
        assert [x for x in result.splitlines() if x.startswith("a=fingerprint:")] == [
            x for x in original if x.startswith("a=fingerprint:sha-256 ")
        ]
        assert [
            x for x in result.splitlines() if not x.startswith("a=fingerprint:")
        ] == [x for x in original if not x.startswith("a=fingerprint:")]
    finally:
        await pc.close()


async def test_session_waits_for_decoded_frame_and_reports_failure():
    session = media.VideoSession(AsyncMock(), "cam", "HD")
    try:
        session.frame, session.sequence = frame(), 1
        seq, actual = await session.next_frame(0)
        assert seq == 1 and actual.width == 64
        session.failure = "disconnected"
        with pytest.raises(ComelitError, match="disconnected"):
            await session.next_frame(0)
    finally:
        await session.close()


async def test_frame_timeout_invalidates_session_and_reports_transport_state():
    session = media.VideoSession(AsyncMock(), "cam", "HD")
    try:
        with pytest.raises(ComelitError, match="timeout.*connection=new.*ICE=new"):
            await session.next_frame(0, timeout=0.01)
        assert session.failure
        assert "video frames=0" in session.failure
        assert session.changed.is_set()
        # Even a late frame must not turn a failed connection into a usable one.
        session.frame, session.sequence = frame(), 1
        with pytest.raises(ComelitError, match="timeout"):
            await session.next_frame(0)
    finally:
        await session.close()


async def test_rejected_offer_closes_peer(monkeypatch):
    monkeypatch.setattr(media, "ICE_SERVERS", ())
    client = AsyncMock()
    client.offer.side_effect = ComelitError("rejected")
    session = media.VideoSession(client, "cam", "HD")
    with pytest.raises(ComelitError):
        await session.start()
    assert session.closed
    assert session.pc.connectionState == "closed"
    assert not session._readers


async def test_shared_session_idle_cleanup_and_reconnect(monkeypatch):
    instances = []

    class FakeSession:
        def __init__(self, *args):
            self.closed = False
            self.failure = None
            instances.append(self)

        async def start(self):
            await asyncio.sleep(0)

        async def image(self):
            await asyncio.sleep(0)
            return b"image"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(media, "VideoSession", FakeSession)
    monkeypatch.setattr(media, "IDLE_TIMEOUT", 0.01)
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    await asyncio.gather(manager.image(), manager.image())
    assert len(instances) == 1
    await asyncio.sleep(0.03)
    assert instances[0].closed
    await manager.image()
    assert len(instances) == 2
    await manager.close()
    assert instances[1].closed
    with pytest.raises(ComelitError, match="shutting down"):
        await manager.image()


@pytest.mark.parametrize("ticks", [3000, 3600, 6000])
def test_mp4_is_decodable_and_input_timestamps_not_mutated(tmp_path, ticks):
    output = tmp_path / "camera.mp4"
    output.write_bytes(b"old")
    writer = media._Recording(output)
    writer.open()
    try:
        for index in range(30):
            sample = frame(index)
            sample.pts = index * ticks
            original_pts = sample.pts
            writer.write(sample)
            assert sample.pts == original_pts
        assert output.read_bytes() == b"old"
        writer.finish()
        with av.open(str(output)) as container:
            assert len(list(container.decode(video=0))) == 30
    finally:
        writer.cleanup()
    assert not list(tmp_path.glob(".comelit-*"))


async def test_failed_recording_preserves_existing_file(monkeypatch, tmp_path):
    class BrokenSession:
        failure = None
        closed = False

        async def start(self):
            pass

        async def close(self):
            self.closed = True

        async def next_frame(self, sequence):
            if sequence:
                raise ComelitError("disconnected")
            return 1, frame()

    monkeypatch.setattr(media, "VideoSession", lambda *args: BrokenSession())
    output = tmp_path / "camera.mp4"
    output.write_bytes(b"previous recording")
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    try:
        with pytest.raises(ComelitError):
            await manager.record(output, 1)
        assert output.read_bytes() == b"previous recording"
        assert not list(tmp_path.glob(".comelit-*"))
    finally:
        await manager.close()


@pytest.mark.parametrize("codec", ["video/VP8", "video/H264"])
async def test_real_local_webrtc_control_and_images(monkeypatch, tmp_path, codec):
    """A real aiortc peer supplies synthetic video and accepts Connexus RPC."""
    monkeypatch.setattr(media, "ICE_SERVERS", ())
    remote = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    messages = []
    channels = []

    @remote.on("datachannel")
    def channel_open(channel):
        channels.append(channel.label)

        @channel.on("message")
        def message_received(message):
            messages.append(message)

    remote.addTrack(VideoStreamTrack())
    remote.getTransceivers()[0].setCodecPreferences(
        [
            item
            for item in RTCRtpSender.getCapabilities("video").codecs
            if item.mimeType == codec
        ]
    )

    class LocalClient:
        async def offer(self, endpoint_id, session_id, sdp):
            from aiortc import RTCSessionDescription

            await remote.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type="offer")
            )
            await remote.setLocalDescription(await remote.createAnswer())
            return remote.localDescription.sdp

    session = media.VideoSession(LocalClient(), "fake-camera", "HD")
    try:
        await session.start()
        image = await session.image()
        assert image.startswith(b"\xff\xd8")
        assert session.sequence > 0
        assert sorted(channels) == ["iu-channel", "libcomelit-channel"]
        assert any("set-video-resolution" in message for message in messages)
        manager = media.VideoManager(LocalClient(), "fake-camera", "HD")
        manager._session = session
        try:
            await manager.record(tmp_path / "local.mp4", 1)
            with av.open(str(tmp_path / "local.mp4")) as container:
                assert len(list(container.decode(video=0))) > 10
        finally:
            await manager.close()
    finally:
        await session.close()
        await remote.close()


async def test_thumbnail_timeout_does_not_restart_camera(monkeypatch):
    ready = asyncio.Event()
    instances = []

    class SlowSession:
        failure = None
        closed = False

        def __init__(self, *args):
            instances.append(self)

        async def start(self):
            await ready.wait()

        async def image(self):
            return b"jpeg"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(media, "VideoSession", SlowSession)
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager.image(), timeout=0.01)
        ready.set()
        assert await manager.image() == b"jpeg"
        assert len(instances) == 1
    finally:
        await manager.close()
    assert instances[0].closed


async def test_abandoned_thumbnail_failure_does_not_log_unhandled_error(monkeypatch):
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    manager = media.VideoManager(AsyncMock(), "cam", "HD")

    async def fail_later():
        await ready.wait()
        raise ComelitError("No video frames arrived before the timeout")

    monkeypatch.setattr(manager, "_image", fail_later)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager.image(), timeout=0.01)
        ready.set()
        await asyncio.wait({manager._image_task})
        await asyncio.sleep(0)
        assert not errors
    finally:
        await manager.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("error_type", [ComelitError, ComelitAuthError])
async def test_failed_start_is_rate_limited_and_later_recovers(monkeypatch, error_type):
    attempts = 0

    class Session:
        failure = None
        closed = False

        async def start(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error_type("Comelit request failed")

        async def image(self):
            return b"jpeg"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(media, "VideoSession", lambda *args: Session())
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    try:
        with pytest.raises(error_type, match="request failed"):
            await manager.image()
        for _ in range(3):
            with pytest.raises(error_type, match="cooling down"):
                await manager.image()
        assert attempts == 1
        # Simulate the cooldown expiring without a real 30-second delay.
        manager._retry_after = 0
        assert await manager.image() == b"jpeg"
        assert attempts == 2
        assert manager._retry_error is None
    finally:
        await manager.close()


async def test_stalled_active_session_closes_then_reconnects(monkeypatch):
    stalled = AsyncMock()
    stalled.failure = None
    stalled.closed = False
    stalled.image.side_effect = ComelitError("No video frames arrived")
    replacement = AsyncMock()
    replacement.failure = None
    replacement.closed = False
    replacement.image.return_value = b"jpeg"
    monkeypatch.setattr(media, "VideoSession", lambda *args: replacement)
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    manager._session = stalled
    try:
        with pytest.raises(ComelitError, match="No video"):
            await manager.image()
        stalled.close.assert_awaited_once()
        assert manager._session is None
        with pytest.raises(ComelitError, match="cooling down"):
            await manager.image()
        replacement.start.assert_not_awaited()
        manager._retry_after = 0
        assert await manager.image() == b"jpeg"
        replacement.start.assert_awaited_once()
    finally:
        await manager.close()


async def test_shutdown_cancels_recording_and_removes_temporary_output(
    monkeypatch, tmp_path
):
    waiting = asyncio.Event()

    class WaitingSession:
        failure = None
        closed = False

        async def start(self):
            pass

        async def close(self):
            self.closed = True

        async def next_frame(self, sequence):
            if not sequence:
                return 1, frame()
            waiting.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(media, "VideoSession", lambda *args: WaitingSession())
    manager = media.VideoManager(AsyncMock(), "cam", "HD")
    output = tmp_path / "out.mp4"
    output.write_bytes(b"old")
    task = asyncio.create_task(manager.record(output, 20))
    await asyncio.wait_for(waiting.wait(), timeout=3)
    await manager.close()
    assert task.cancelled()
    assert output.read_bytes() == b"old"
    assert not list(tmp_path.glob(".comelit-*"))
