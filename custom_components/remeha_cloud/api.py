"""Async client for the Remeha Home cloud API (BDR Thermea).

Endpoints: https://github.com/msvisser/remeha_home/blob/main/documentation/api.md
Login: Azure AD B2C authorization-code + PKCE flow, scripted with the app's
public client id (the same flow the Remeha Home app performs).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import datetime
import hashlib
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.bdrthermea.net/Mobile/api"
SUBSCRIPTION_KEY = "df605c5470d846fc91e848b1cc653ddf"

_B2C = "https://remehalogin.bdrthermea.net/bdrb2cprod.onmicrosoft.com"
_POLICY = "B2C_1A_RPSignUpSignInNewRoomV3.1"
_POLICY_PATH = "B2C_1A_RPSignUpSignInNewRoomv3.1"  # lower-case "v" as used by the app
_CLIENT_ID = "6ce007c6-0628-419e-88f4-bee2e6418eec"
_REDIRECT_URI = "com.b2c.remehaapp://login-callback"
_SCOPE = (
    "openid https://bdrb2cprod.onmicrosoft.com/iotdevice/user_impersonation "
    "offline_access"
)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_TOKEN_MARGIN = 120  # refresh this many seconds before expiry

DHW_MODE_ECO = "anti-frost"
DHW_MODE_SCHEDULE = "schedule"
DHW_MODE_COMFORT = "continuous-comfort"


class RemehaError(Exception):
    """Base error."""


class RemehaAuthError(RemehaError):
    """Credentials or tokens rejected."""


class RemehaConnectionError(RemehaError):
    """Network / HTTP error."""


class RemehaLoginFlowError(RemehaError):
    """Credentials accepted, but a later step of the B2C login failed."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


_SETTINGS_RE = re.compile(r"var\s+SETTINGS\s*=\s*(\{.*?\});", re.DOTALL)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_B2C_CODE_RE = re.compile(r"AADB2C\d+[^<\"]{0,120}")


def _page_settings(page: str) -> dict[str, Any] | None:
    """Return the SETTINGS object a B2C page embeds, if any."""
    if not (match := _SETTINGS_RE.search(page)):
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _page_summary(page: str) -> str:
    """Short, password-free description of an HTML page for the log."""
    title = _TITLE_RE.search(page)
    codes = sorted(set(_B2C_CODE_RE.findall(page)))[:3]
    return f"title={title.group(1).strip() if title else '-'!r}, b2c_errors={codes}"


_INPUT_RE = re.compile(r"<input[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'\b(id|name|type)="([^"]*)"', re.IGNORECASE)
_SECRET_KEYS = {"csrf", "transId", "pageViewId", "remoteResource"}


def _page_details(page: str, settings: dict[str, Any] | None) -> str:
    """Structure of a B2C page (inputs, SETTINGS keys) without secrets."""
    inputs = []
    for tag in _INPUT_RE.findall(page)[:15]:
        attrs = dict((k.lower(), v) for k, v in _ATTR_RE.findall(tag))
        if attrs.get("type") != "hidden" or attrs.get("id"):
            inputs.append(f"{attrs.get('type', '?')}:{attrs.get('id') or attrs.get('name')}")
    info: dict[str, Any] = {"inputs": inputs, "length": len(page)}
    if settings:
        info["settings"] = {
            k: v for k, v in settings.items()
            if k not in _SECRET_KEYS and not isinstance(v, (dict, list))
        }
        info["settings_objects"] = sorted(
            k for k, v in settings.items() if isinstance(v, (dict, list))
        )
        info["config"] = settings.get("config")
    return json.dumps(info, default=str)[:1500]


class RemehaClient:
    """Remeha Home API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        token: dict[str, Any] | None = None,
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token = token
        self._on_token_update = on_token_update
        self._token_lock = asyncio.Lock()

    @property
    def token(self) -> dict[str, Any] | None:
        """Current token set."""
        return self._token

    # ------------------------------------------------------------------ auth

    async def async_login(self) -> None:
        """Run the full B2C login with email/password and obtain tokens."""
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        try:
            # Dedicated session so the B2C cookies (CSRF) are isolated and discarded.
            # Inherit the shared session's default headers (Home Assistant's
            # User-Agent), like the remeha_home login that works for this account.
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(quote_cookie=False),
                timeout=_TIMEOUT,
                headers=dict(self._session.headers),
            ) as login_session:
                code = await self._async_authorize(login_session, challenge)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RemehaConnectionError(f"Login request failed: {err}") from err

        try:
            await self._async_token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _REDIRECT_URI,
                    "code_verifier": verifier,
                    "client_id": _CLIENT_ID,
                }
            )
        except RemehaAuthError as err:
            raise RemehaLoginFlowError(f"Code exchange failed: {err}") from err

    async def _async_authorize(
        self, session: aiohttp.ClientSession, challenge: str
    ) -> str:
        """Walk the B2C pages and return an authorization code.

        The URLs, transaction id and CSRF token for the next steps are taken from
        the SETTINGS object that the B2C login page embeds, so they always match
        what the page itself would post (exact policy name and casing included).
        """
        async with session.get(
            f"{_B2C}/oauth2/v2.0/authorize",
            params={
                "response_type": "code",
                "client_id": _CLIENT_ID,
                "redirect_uri": _REDIRECT_URI,
                "scope": _SCOPE,
                "state": secrets.token_urlsafe(),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "p": _POLICY,
                "brand": "remeha",
                "lang": "en",
                "nonce": "defaultNonce",
                "prompt": "login",
                "signUp": "False",
            },
        ) as resp:
            resp.raise_for_status()
            page = await resp.text()
            page_url = resp.url
            request_id = resp.headers.get("x-request-id")

        settings = _page_settings(page)
        origin = f"{page_url.scheme}://{page_url.host}"
        cookie_csrf = next(
            (c.value for c in session.cookie_jar if c.key == "x-ms-cpim-csrf"), None
        )
        tid_tx = None
        if request_id:
            state = json.dumps({"TID": request_id}, separators=(",", ":"))
            tid_tx = "StateProperties=" + _b64url(state.encode("ascii"))

        settings = settings or {}
        hosts = settings.get("hosts") or {}
        tenant = hosts.get("tenant") or f"/bdrb2cprod.onmicrosoft.com/{_POLICY_PATH}"
        policy = hosts.get("policy") or _POLICY_PATH
        api_name = settings.get("api") or "CombinedSigninAndSignup"
        tx = tid_tx or settings.get("transId")
        csrf = cookie_csrf or settings.get("csrf")
        _LOGGER.debug(
            "B2C ids: tx_from_request_id=%s tx_matches_settings=%s "
            "csrf_from_cookie=%s csrf_matches_settings=%s xhrSettings=%s",
            bool(tid_tx), tid_tx == settings.get("transId"),
            bool(cookie_csrf), cookie_csrf == settings.get("csrf"),
            settings.get("xhrSettings"),
        )
        user_field = "email" if 'id="email"' in page and 'id="signInName"' not in page else "signInName"
        _LOGGER.debug(
            "B2C login page: url=%s settings=%s tenant=%s policy=%s api=%s "
            "user_field=%s tx=%s csrf=%s cookies=%s",
            page_url.path, bool(settings), tenant, policy, api_name, user_field,
            bool(tx), bool(csrf), sorted({c.key for c in session.cookie_jar}),
        )
        _LOGGER.debug("B2C login page details: %s", _page_details(page, settings))
        if not tx or not csrf:
            raise RemehaLoginFlowError(
                f"Login page not understood ({_page_summary(page)})"
            )

        # Same request the page's own JavaScript (jQuery $.ajax) sends.
        async with session.post(
            f"{origin}{tenant}/SelfAsserted",
            params={"tx": tx, "p": policy},
            headers={
                "x-csrf-token": csrf,
                "x-requested-with": "XMLHttpRequest",
                "accept": "application/json, text/javascript, */*; q=0.01",
                "origin": origin,
                "referer": str(page_url),
            },
            data={
                "request_type": "RESPONSE",
                user_field: self._email,
                "password": self._password,
            },
            allow_redirects=False,
        ) as resp:
            body = await resp.text()
            self_asserted_status = resp.status
            _LOGGER.debug(
                "B2C SelfAsserted response: HTTP %s, content-type=%s, location=%s",
                resp.status, resp.headers.get("content-type"),
                resp.headers.get("location", "-").split("?")[0],
            )

        try:
            result = json.loads(body)
        except ValueError:
            raise RemehaLoginFlowError(
                f"SelfAsserted returned no JSON (HTTP {self_asserted_status}, "
                f"{_page_summary(body)}, page: "
                f"{_page_details(body, _page_settings(body))})"
            ) from None
        status = str(result.get("status"))
        _LOGGER.debug("B2C SelfAsserted: status %s, message %s", status, result.get("message"))
        if status != "200":
            # B2C states the reason, e.g. "Your password is incorrect."
            raise RemehaAuthError(
                f"Login rejected (status {status}): {result.get('message') or body[:200]}"
            )

        async with session.get(
            f"{origin}{tenant}/api/{api_name}/confirmed",
            params={"rememberMe": "false", "csrf_token": csrf, "tx": tx, "p": policy},
            allow_redirects=False,
        ) as resp:
            location = resp.headers.get("location", "")
            confirmed_status = resp.status
            confirmed_body = "" if location else (await resp.text())[:300]

        query = parse_qs(urlparse(location).query)
        code = query.get("code", [None])[0]
        _LOGGER.debug(
            "B2C confirmed: HTTP %s, redirect to %s, code present: %s",
            confirmed_status, location.split("?")[0], bool(code),
        )
        if not code:
            raise RemehaLoginFlowError(
                f"No authorization code (HTTP {confirmed_status}, "
                f"error={query.get('error', ['-'])[0]}, "
                f"description={query.get('error_description', ['-'])[0]}, "
                f"body={confirmed_body!r})"
            )
        return code

    async def _async_token_request(self, data: dict[str, str]) -> None:
        try:
            async with self._session.post(
                f"{_B2C}/oauth2/v2.0/token",
                params={"p": _POLICY},
                data=data,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (400, 401):
                    body = await resp.text()
                    raise RemehaAuthError(f"Token request rejected: {body[:300]}")
                resp.raise_for_status()
                token: dict[str, Any] = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RemehaConnectionError(f"Token request failed: {err}") from err

        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        if "refresh_token" not in token and self._token:
            token["refresh_token"] = self._token.get("refresh_token")
        self._token = token
        if self._on_token_update:
            self._on_token_update(token)

    async def _async_access_token(self, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if not self._token:
                await self.async_login()
            elif force_refresh or (
                self._token.get("expires_at", 0) - _TOKEN_MARGIN < time.time()
            ):
                try:
                    await self._async_token_request(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self._token.get("refresh_token", ""),
                            "client_id": _CLIENT_ID,
                        }
                    )
                except RemehaAuthError:
                    # Known B2C quirk: refresh sometimes 400s. Fall back to full login.
                    _LOGGER.info("Remeha refresh token rejected, logging in again")
                    await self.async_login()
            return self._token["access_token"]

    # --------------------------------------------------------------- requests

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        token = await self._async_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
            "Accept": "application/json",
        }
        try:
            async with self._session.request(
                method,
                API_BASE + path,
                json=json_body,
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    if not retry:
                        raise RemehaAuthError("API returned 401 after token refresh")
                else:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RemehaConnectionError(
                            f"{method} {path} -> HTTP {resp.status}: {body[:300]}"
                        )
                    text = await resp.text()
                    return json.loads(text) if text.strip() else None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise RemehaConnectionError(f"{method} {path} failed: {err}") from err

        # 401 on first attempt: force token refresh and retry once.
        await self._async_access_token(force_refresh=True)
        return await self._async_request(
            method, path, json_body=json_body, params=params, retry=False
        )

    # ------------------------------------------------------------- endpoints

    async def async_get_dashboard(self) -> dict[str, Any]:
        """GET /homes/dashboard (timestamp param defeats caching)."""
        return await self._async_request(
            "GET", "/homes/dashboard", params={"t": str(int(time.time()))}
        ) or {}

    async def async_get_technical_details(self, appliance_id: str) -> dict[str, Any]:
        """GET /appliances/{id}/technicaldetails."""
        return await self._async_request(
            "GET", f"/appliances/{appliance_id}/technicaldetails"
        ) or {}

    async def async_get_energy_consumption(
        self,
        appliance_id: str,
        start: datetime,
        end: datetime,
        period: str = "daily",
    ) -> dict[str, Any]:
        """GET /appliances/{id}/energyconsumption/{daily|monthly|yearly}."""
        fmt = "%Y-%m-%d %H:%M:%S.000Z"
        return await self._async_request(
            "GET",
            f"/appliances/{appliance_id}/energyconsumption/{period}",
            params={"startDate": start.strftime(fmt), "endDate": end.strftime(fmt)},
        ) or {}

    # climate zones (read)
    async def async_get_heating_curve(self, zone_id: str) -> dict[str, Any]:
        """GET /climate-zones/{id}/heating-curve (slope, base setpoint, max flow temp)."""
        return await self._async_request("GET", f"/climate-zones/{zone_id}/heating-curve") or {}

    async def async_get_activities(self, zone_id: str) -> list[dict[str, Any]]:
        """GET /climate-zones/{id}/activities (named temperatures used by the schedule)."""
        return await self._async_request("GET", f"/climate-zones/{zone_id}/activities") or []

    # climate zones
    async def async_zone_manual(self, zone_id: str, setpoint: float) -> None:
        await self._async_request(
            "POST",
            f"/climate-zones/{zone_id}/modes/manual",
            json_body={"roomTemperatureSetPoint": setpoint},
        )

    async def async_zone_schedule(self, zone_id: str, program: int) -> None:
        await self._async_request(
            "POST",
            f"/climate-zones/{zone_id}/modes/schedule",
            json_body={"heatingProgramId": program},
        )

    async def async_zone_temporary_override(self, zone_id: str, setpoint: float) -> None:
        await self._async_request(
            "POST",
            f"/climate-zones/{zone_id}/modes/temporary-override",
            json_body={"roomTemperatureSetPoint": setpoint},
        )

    async def async_zone_off(self, zone_id: str) -> None:
        await self._async_request("POST", f"/climate-zones/{zone_id}/modes/anti-frost")

    async def async_zone_activate_program(self, zone_id: str, program: int) -> None:
        await self._async_request(
            "POST",
            f"/climate-zones/{zone_id}/time-programs/heating/{program}/activate",
        )

    async def async_zone_fireplace(self, zone_id: str, active: bool) -> None:
        await self._async_request(
            "POST",
            f"/climate-zones/{zone_id}/modes/fireplacemode",
            json_body={"fireplaceModeActive": active},
        )

    # hot water zones
    async def async_dhw_mode(self, zone_id: str, mode: str) -> None:
        """mode: DHW_MODE_ECO | DHW_MODE_SCHEDULE | DHW_MODE_COMFORT."""
        await self._async_request("POST", f"/hot-water-zones/{zone_id}/modes/{mode}")

    async def async_dhw_comfort_setpoint(self, zone_id: str, value: float) -> None:
        await self._async_request(
            "POST",
            f"/hot-water-zones/{zone_id}/comfort-setpoint",
            json_body={"comfortSetpoint": value},
        )

    async def async_dhw_reduced_setpoint(self, zone_id: str, value: float) -> None:
        await self._async_request(
            "POST",
            f"/hot-water-zones/{zone_id}/reduced-setpoint",
            json_body={"reducedSetpoint": value},
        )
