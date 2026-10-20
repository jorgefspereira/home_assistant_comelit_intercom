"""Async Comelit cloud API, independent of Home Assistant."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from .const import BASE_URL, CLIENT_ID, REDIRECT_URI


class ComelitError(Exception):
    """A sanitized error safe to display without response bodies or credentials."""


class ComelitAuthError(ComelitError):
    """Credentials or authorization are invalid."""


@dataclass(frozen=True)
class Endpoint:
    """One discovered device-com endpoint."""

    id: str
    name: str
    capabilities: tuple[str, ...]
    categories: tuple[str, ...]

    @property
    def camera(self) -> bool:
        return "RTCSessionController" in self.capabilities

    @property
    def power(self) -> bool:
        return "PowerController" in self.capabilities


class ComelitClient:
    """Use a caller-owned HTTP session; keep access tokens only in memory."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str):
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._expires = 0.0
        self._auth_lock = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            async with self._session.request(
                method,
                BASE_URL + path,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                # Never log bodies, URLs from errors, authorization headers or SDP.
                if response.status in (400, 401, 403) and path.startswith("/o-auth-2/"):
                    raise ComelitAuthError("Comelit rejected the account credentials")
                if response.status in (401, 403):
                    raise ComelitAuthError(
                        "Comelit authorization expired or access was denied"
                    )
                if path == "/o-auth-2/auth" and response.status in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ):
                    return {"location": response.headers.get("Location", "")}
                if not 200 <= response.status < 300:
                    raise ComelitError(
                        f"Comelit request failed (HTTP {response.status})"
                    )
                body = await response.text()
                if not body:
                    return None
                try:
                    return json.loads(body)
                except (ValueError, TypeError):
                    raise ComelitError("Comelit returned an invalid response") from None
        except (aiohttp.ClientError, TimeoutError):
            raise ComelitError("Cannot communicate with Comelit") from None

    async def _authenticate(self) -> str:
        async with self._auth_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            verifier = secrets.token_urlsafe(32)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            state = secrets.token_urlsafe(16)
            auth = await self._request(
                "POST",
                "/o-auth-2/auth",
                json={
                    "username": self._email,
                    "password": self._password,
                    "responseType": "code",
                    "clientId": CLIENT_ID,
                    "redirectUri": REDIRECT_URI,
                    "scope": "all",
                    "state": state,
                    "codeChallenge": challenge,
                    "codeChallengeMethod": "S256",
                },
            )
            location = auth.get("location") if isinstance(auth, dict) else None
            if not isinstance(location, str):
                raise ComelitAuthError("Comelit did not return an authorization code")
            parsed = urlparse(location)
            expected = urlparse(REDIRECT_URI)
            query = parse_qs(parsed.query)
            if (
                (parsed.scheme, parsed.netloc, parsed.path)
                != (expected.scheme, expected.netloc, expected.path)
                or query.get("state") != [state]
                or len(query.get("code", [])) != 1
            ):
                raise ComelitAuthError("Invalid Comelit authorization redirect")
            token = await self._request(
                "POST",
                "/o-auth-2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "scope": "all",
                    "code": query["code"][0],
                    "code_verifier": verifier,
                },
            )
            if not isinstance(token, dict) or not isinstance(
                token.get("access_token"), str
            ):
                raise ComelitAuthError("Comelit did not return an access token")
            try:
                lifetime = float(token.get("expires_in", 3600))
            except (ValueError, TypeError):
                raise ComelitError("Invalid token lifetime") from None
            self._token = token["access_token"]
            self._expires = time.monotonic() + max(0, lifetime - 60)
            return self._token

    async def _authorized(self, method: str, path: str, **kwargs):
        for attempt in range(2):
            token = await self._authenticate()
            try:
                return await self._request(
                    method,
                    path,
                    headers={
                        "Authorization": f"bearer {token}",
                        "User-Agent": "okhttp/4.12.0",
                    },
                    **kwargs,
                )
            except ComelitAuthError:
                if self._token == token:
                    self._token = None
                # Retry only discovery. Never replay a physical command or RTC offer.
                if method != "GET" or attempt:
                    raise
        raise ComelitAuthError("Comelit authorization failed")

    async def discover(self) -> list[Endpoint]:
        data = await self._authorized(
            "GET", "/servicerest/devicecom/endpoints/discovery"
        )
        if not isinstance(data, list):
            raise ComelitError("Invalid device discovery response")
        result = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(
                item.get("endpointId"), str
            ):
                raise ComelitError("Invalid endpoint in discovery response")
            capabilities = item.get("capabilities", [])
            categories = item.get("displayCategories", [])
            if not all(
                isinstance(value, list) and all(isinstance(x, str) for x in value)
                for value in (capabilities, categories)
            ):
                raise ComelitError("Invalid endpoint capabilities")
            result.append(
                Endpoint(
                    item["endpointId"],
                    item.get("friendlyName") or "Comelit",
                    tuple(capabilities),
                    tuple(categories),
                )
            )
        return result

    async def release(self, endpoint: Endpoint) -> None:
        if not endpoint.power:
            raise ComelitError(
                "This endpoint does not support door or relay activation"
            )
        await self._authorized(
            "PUT", self._path(endpoint.id, "power"), json={"value": True}
        )

    async def offer(self, endpoint_id: str, session_id: str, sdp: str) -> str:
        data = await self._authorized(
            "PUT",
            self._path(endpoint_id, "rtc/offer"),
            json={
                "sessionId": session_id,
                "offer": sdp,
                "callId": None,
            },
        )
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("answer"), str)
            or not data["answer"]
        ):
            raise ComelitError("Comelit did not return a video session answer")
        return data["answer"]

    @staticmethod
    def _path(endpoint_id: str, action: str) -> str:
        return f"/servicerest/devicecom/endpoint/{quote(endpoint_id, safe='')}/{action}"
