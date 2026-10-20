"""On-demand WebRTC video, JPEG previews and atomic MP4 recording."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from fractions import Fraction
from io import BytesIO
from pathlib import Path

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError

from .api import ComelitClient, ComelitError
from .const import (
    FRAME_TIMEOUT,
    ICE_SERVERS,
    IDLE_TIMEOUT,
    RETRY_DELAY,
    STARTUP_TIMEOUT,
)


def compatible_offer(sdp: str) -> str:
    """Preserve the real certificate's SHA-256 fingerprint and all other SDP."""
    return (
        "\r\n".join(
            line
            for line in sdp.splitlines()
            if not line.startswith("a=fingerprint:")
            or line.startswith("a=fingerprint:sha-256 ")
        )
        + "\r\n"
    )


def _jpeg(frame: av.VideoFrame) -> bytes:
    output = BytesIO()
    frame.to_image().save(output, format="JPEG", quality=85)
    return output.getvalue()


class VideoSession:
    """One monitor connection. A track event alone does not imply usable video."""

    def __init__(self, client: ComelitClient, endpoint_id: str, resolution: str):
        self.client = client
        self.endpoint_id = endpoint_id
        self.resolution = resolution
        self.pc = RTCPeerConnection(
            RTCConfiguration(
                iceServers=[RTCIceServer(urls=[url]) for url in ICE_SERVERS]
            )
        )
        self.frame: av.VideoFrame | None = None
        self.sequence = 0
        self.changed = asyncio.Event()
        self.failure: str | None = None
        self.closed = False
        self._readers: set[asyncio.Task] = set()
        self._jpeg_lock = asyncio.Lock()
        self._jpeg: bytes | None = None
        self._jpeg_at = 0.0
        self._channels = {}

    def _timeout_message(self, message: str) -> str:
        """Report transport progress without SDP, addresses or credentials."""
        channels = ", ".join(
            f"{name}={channel.readyState}"
            for name, channel in self._channels.items()
        ) or "not created"
        return (
            f"{message} (connection={self.pc.connectionState}, "
            f"ICE={self.pc.iceConnectionState}, channels: {channels}, "
            f"video frames={self.sequence})"
        )

    async def start(self) -> None:
        @self.pc.on("track")
        def on_track(track):
            task = asyncio.create_task(self._read(track))
            self._readers.add(task)
            task.add_done_callback(self._readers.discard)

        @self.pc.on("connectionstatechange")
        def on_state():
            if self.pc.connectionState in ("failed", "closed"):
                self.failure = "Video connection closed"
                self.changed.set()

        for label in ("libcomelit-channel", "iu-channel"):
            channel = self.pc.createDataChannel(label, ordered=True)
            self._channels[label] = channel
            if label == "libcomelit-channel":

                @channel.on("open")
                def on_open(channel=channel):
                    channel.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "set-video-resolution",
                                "params": {"resolution": self.resolution},
                                "id": 1,
                            }
                        )
                    )

                @channel.on("message")
                def on_message(message):
                    try:
                        response = json.loads(message)
                        if response.get("id") == 1 and (
                            response.get("error")
                            or (
                                isinstance(response.get("result"), dict)
                                and response["result"].get("rescode", 0) != 0
                            )
                        ):
                            self.failure = "Monitor rejected the video start command"
                            self.changed.set()
                    except (ValueError, TypeError, AttributeError):
                        pass

        self.pc.addTransceiver("video", direction="recvonly")
        self.pc.addTransceiver("audio", direction="recvonly")
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await self.pc.setLocalDescription(await self.pc.createOffer())
                answer = await self.client.offer(
                    self.endpoint_id,
                    str(uuid.uuid4()),
                    compatible_offer(self.pc.localDescription.sdp),
                )
                await self.pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer, type="answer")
                )
                await self.next_frame(0, timeout=30)
        except TimeoutError:
            message = self._timeout_message("Video startup timed out")
            await self.close()
            raise ComelitError(message) from None
        except BaseException:
            await self.close()
            raise

    async def _read(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                if track.kind == "video":
                    self.frame = frame
                    self.sequence += 1
                    self.changed.set()
                # Drain negotiated audio without requiring audio frames for success.
        except asyncio.CancelledError:
            raise
        except (MediaStreamError, RuntimeError, ValueError):
            if track.kind == "video":
                self.failure = "Video frames stopped arriving"
                self.changed.set()

    async def next_frame(self, after: int, timeout: float = FRAME_TIMEOUT):
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if self.failure or self.closed:
                        raise ComelitError(self.failure or "Video session closed")
                    if self.sequence > after and self.frame is not None:
                        return self.sequence, self.frame
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            self.failure = self._timeout_message(
                "No video frames arrived before the timeout"
            )
            self.changed.set()
            raise ComelitError(self.failure) from None

    async def image(self) -> bytes:
        async with self._jpeg_lock:
            now = asyncio.get_running_loop().time()
            if (
                self._jpeg is not None
                and now - self._jpeg_at < 0.25
                and not self.failure
            ):
                return self._jpeg
            # Require a fresh frame, rather than serving a frozen image indefinitely.
            _, frame = await self.next_frame(self.sequence)
            self._jpeg = await asyncio.to_thread(_jpeg, frame)
            self._jpeg_at = asyncio.get_running_loop().time()
            return self._jpeg

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.changed.set()
        tasks = list(self._readers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.pc.close()


class _Recording:
    """All methods run sequentially in the executor, including disk I/O and codecs."""

    def __init__(self, destination: Path):
        self.destination = destination
        self.pending: Path | None = None
        self.container = None
        self.stream = None
        self.first_time = None
        self.last_pts = -1

    def open(self) -> None:
        fd, name = tempfile.mkstemp(
            prefix=".comelit-", suffix=".mp4", dir=self.destination.parent
        )
        os.close(fd)
        self.pending = Path(name)

    def write(self, frame: av.VideoFrame) -> None:
        if self.container is None:
            self.container = av.open(str(self.pending), mode="w", format="mp4")
            self.stream = self.container.add_stream("libx264", rate=25)
            self.stream.width, self.stream.height = frame.width, frame.height
            self.stream.pix_fmt = "yuv420p"
            # Retain RTP timing rather than rounding to the nominal 25 fps rate.
            # Otherwise 30 fps input can produce duplicate DTS values in MP4.
            self.stream.time_base = Fraction(1, 90000)
            self.stream.codec_context.time_base = Fraction(1, 90000)
            self.stream.options = {"preset": "ultrafast", "crf": "23"}
        # Copy: do not mutate frames shared with snapshots or another recorder.
        copy = av.VideoFrame.from_image(frame.to_image())
        stamp = (
            float(frame.time)
            if frame.time is not None
            else self.last_pts / 90000 + 0.04
        )
        if self.first_time is None:
            self.first_time = stamp
        copy.pts = max(self.last_pts + 1, round((stamp - self.first_time) * 90000))
        copy.time_base = Fraction(1, 90000)
        self.last_pts = copy.pts
        for packet in self.stream.encode(copy):
            self.container.mux(packet)

    def finish(self) -> None:
        if self.container is None:
            raise ComelitError("Recording contains no video")
        for packet in self.stream.encode(None):
            self.container.mux(packet)
        self.container.close()
        self.container = None
        with av.open(str(self.pending)) as check:
            if not check.streams.video or next(check.decode(video=0), None) is None:
                raise ComelitError("Recording contains no decodable video")
        os.replace(self.pending, self.destination)

    def cleanup(self) -> None:
        try:
            if self.container is not None:
                self.container.close()
        finally:
            if self.pending:
                self.pending.unlink(missing_ok=True)


async def _io(function, *args):
    """Let in-flight disk/codec work finish before cancellation cleanup touches it."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class VideoManager:
    """Share a connection per endpoint and close it after the last consumer."""

    def __init__(self, client: ComelitClient, endpoint_id: str, resolution: str):
        self.client, self.endpoint_id, self.resolution = client, endpoint_id, resolution
        self._session: VideoSession | None = None
        self._lock = asyncio.Lock()
        self._users = 0
        self._idle: asyncio.Task | None = None
        self._closed = False
        self._record_lock = asyncio.Lock()
        self._consumers: set[asyncio.Task] = set()
        self._image_task: asyncio.Task | None = None
        self._retry_after = 0.0
        self._retry_error: tuple[type[ComelitError], str] | None = None

    def _defer_retry(self, error: ComelitError) -> None:
        self._retry_after = asyncio.get_running_loop().time() + RETRY_DELAY
        self._retry_error = (type(error), str(error))

    @asynccontextmanager
    async def acquire(self):
        task = asyncio.current_task()
        self._consumers.add(task)
        acquired = False
        session = None
        try:
            async with self._lock:
                if self._closed:
                    raise ComelitError("Video manager is shutting down")
                if self._idle:
                    self._idle.cancel()
                    self._idle = None
                if self._session and (self._session.failure or self._session.closed):
                    self._defer_retry(
                        ComelitError(self._session.failure or "Video session closed")
                    )
                    await self._session.close()
                    self._session = None
                if self._session is None:
                    if (
                        self._retry_error is not None
                        and asyncio.get_running_loop().time() < self._retry_after
                    ):
                        error_type, message = self._retry_error
                        raise error_type(f"Camera reconnect is cooling down: {message}")
                    session = VideoSession(
                        self.client, self.endpoint_id, self.resolution
                    )
                    try:
                        await session.start()
                    except ComelitError as err:
                        self._defer_retry(err)
                        raise
                    self._session = session
                    self._retry_error = None
                self._users += 1
                acquired = True
                session = self._session
            yield session
        except ComelitError as err:
            # A stalled stream must not be reused by the next preview or recording.
            if acquired:
                async with self._lock:
                    if self._session is session:
                        self._session = None
                        self._defer_retry(err)
                        await session.close()
            raise
        finally:
            self._consumers.discard(task)
            if acquired:
                async with self._lock:
                    self._users -= 1
                    if not self._users and not self._closed:
                        self._idle = asyncio.create_task(self._close_idle())

    async def _close_idle(self):
        await asyncio.sleep(IDLE_TIMEOUT)
        async with self._lock:
            if not self._users and self._session:
                session, self._session = self._session, None
                # Do not allow acquire() to cancel an in-progress close.
                self._idle = None
                await session.close()

    async def image(self) -> bytes:
        # HA can abandon a thumbnail after 10 seconds. Let the bounded startup
        # finish so its next request can use the same connection.
        if self._image_task is None or self._image_task.done():
            self._image_task = asyncio.create_task(self._image())
            self._image_task.add_done_callback(self._consume_image_error)
        task = self._image_task
        # wait() leaves the shared task running when a thumbnail is cancelled.
        # Unlike shield() on Python 3.14, it does not log a later background
        # failure as an unhandled exception after that cancellation.
        await asyncio.wait({task})
        return task.result()

    @staticmethod
    def _consume_image_error(task):
        if not task.cancelled():
            task.exception()

    async def _image(self) -> bytes:
        async with self.acquire() as session:
            return await session.image()

    async def record(self, destination: Path, duration: int) -> None:
        if self._record_lock.locked():
            raise ComelitError("A recording is already running for this camera")
        async with self._record_lock, self.acquire() as session:
            writer = _Recording(destination)
            try:
                await _io(writer.open)
                sequence, frame = await session.next_frame(0)
                deadline = asyncio.get_running_loop().time() + duration
                await _io(writer.write, frame)
                while asyncio.get_running_loop().time() < deadline:
                    sequence, frame = await session.next_frame(sequence)
                    await _io(writer.write, frame)
                await _io(writer.finish)
            finally:
                await _io(writer.cleanup)

    async def close(self) -> None:
        self._closed = True
        if self._image_task and not self._image_task.done():
            self._image_task.cancel()
            await asyncio.gather(self._image_task, return_exceptions=True)
        if self._idle:
            self._idle.cancel()
            await asyncio.gather(self._idle, return_exceptions=True)
        tasks = [task for task in self._consumers if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            if self._session:
                await self._session.close()
                self._session = None
