"""Exercise protocol boundaries without real credentials or network access."""

import asyncio
import json
from urllib.parse import urlencode

import pytest
from custom_components.comelit_intercom.api import (
    ComelitAuthError,
    ComelitClient,
    ComelitError,
    Endpoint,
)
from custom_components.comelit_intercom.const import BASE_URL, REDIRECT_URI


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return "" if self.body is None else json.dumps(self.body)


class Session:
    def __init__(self, endpoint_response=None):
        self.calls = []
        self.endpoint_response = endpoint_response or Response(body=[])
        self.auth_count = 0
        self.bad_state = False
        self.redirect = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert url.startswith(BASE_URL)
        assert kwargs["allow_redirects"] is False
        if url.endswith("/auth"):
            self.auth_count += 1
            state = "wrong" if self.bad_state else kwargs["json"]["state"]
            location = (
                REDIRECT_URI + "?" + urlencode({"code": "test-code", "state": state})
            )
            if self.redirect:
                return Response(302, headers={"Location": location})
            return Response(body={"location": location})
        if url.endswith("/token"):
            return Response(body={"access_token": "test-token", "expires_in": 604800})
        if isinstance(self.endpoint_response, list):
            return self.endpoint_response.pop(0)
        return self.endpoint_response


@pytest.mark.parametrize("redirect", [False, True])
async def test_pkce_and_discovery(redirect):
    session = Session(
        Response(
            body=[
                {
                    "endpointId": "a#b",
                    "friendlyName": "Entrada",
                    "capabilities": ["RTCSessionController"],
                    "displayCategories": ["CAMERA"],
                }
            ]
        )
    )
    session.redirect = redirect
    client = ComelitClient(session, "test@example.invalid", "fake-password")
    endpoints = await client.discover()
    assert endpoints[0].camera
    assert not endpoints[0].power
    auth = session.calls[0][2]["json"]
    form = session.calls[1][2]["data"]
    import base64
    import hashlib

    assert auth["codeChallenge"] == base64.urlsafe_b64encode(
        hashlib.sha256(form["code_verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert session.calls[-1][2]["headers"]["Authorization"] == "bearer test-token"
    assert all("fake-password" not in str(call) for call in session.calls[1:])


async def test_invalid_state_stops_before_token_exchange():
    session = Session()
    session.bad_state = True
    with pytest.raises(ComelitAuthError):
        await ComelitClient(session, "x", "y").discover()
    assert len(session.calls) == 1


async def test_concurrent_requests_share_token_and_expiry_renews():
    session = Session()
    client = ComelitClient(session, "x", "y")
    await asyncio.gather(*(client.discover() for _ in range(5)))
    assert session.auth_count == 1
    client._expires = 0
    await client.discover()
    assert session.auth_count == 2


@pytest.mark.parametrize("status", [200, 202, 204])
async def test_release_sends_exactly_one_encoded_true_command(status):
    session = Session(Response(status=status))
    client = ComelitClient(session, "x", "y")
    await client.release(Endpoint("lock#OD/x", "Door", ("PowerController",), ()))
    calls = [call for call in session.calls if call[0] == "PUT"]
    assert len(calls) == 1
    assert calls[0][1].endswith("/lock%23OD%2Fx/power")
    assert calls[0][2]["json"] == {"value": True}


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_release_error_is_not_retried_and_body_is_redacted(status):
    session = Session(Response(status, {"private": "SECRET"}))
    with pytest.raises(ComelitError) as error:
        await ComelitClient(session, "x", "y").release(
            Endpoint("lock", "Door", ("PowerController",), ())
        )
    assert "SECRET" not in str(error.value)
    assert sum(call[0] == "PUT" for call in session.calls) == 1


async def test_discovery_reauth_retries_only_once():
    session = Session([Response(401), Response(body=[])])
    assert await ComelitClient(session, "x", "y").discover() == []
    assert session.auth_count == 2


async def test_release_rejects_camera_without_network():
    session = Session()
    with pytest.raises(ComelitError):
        await ComelitClient(session, "x", "y").release(
            Endpoint("camera", "Camera", ("RTCSessionController",), ())
        )
    assert session.calls == []


async def test_offer_requires_answer_and_never_retries():
    session = Session(Response(body={}))
    with pytest.raises(ComelitError):
        await ComelitClient(session, "x", "y").offer("cam#1", "uuid", "sdp")
    assert session.calls[-1][2]["json"] == {
        "sessionId": "uuid",
        "offer": "sdp",
        "callId": None,
    }
    assert session.calls[-1][1].endswith("/cam%231/rtc/offer")
