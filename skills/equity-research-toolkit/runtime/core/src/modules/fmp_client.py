#!/usr/bin/env python
# Modified before redistribution: local FMP Stable / model failure handling changes
# were present in the source checkout. See the skill-root SOURCE_PROVENANCE.json.
"""Small, safe client for Financial Modeling Prep's Stable API."""

from __future__ import annotations

import re
from typing import Any

import requests


FMP_STABLE_BASE_URL = "https://financialmodelingprep.com/stable"


class FMPAPIError(RuntimeError):
    """FMP request failure that never includes the API key or request URL."""

    def __init__(self, endpoint: str, status_code: int | None, detail: str):
        self.endpoint = endpoint
        self.status_code = status_code
        self.detail = detail
        status = f"HTTP {status_code}" if status_code is not None else "network error"
        super().__init__(f"FMP {endpoint} failed ({status}): {detail}")


def _redact_detail(detail: Any, api_key: str) -> str:
    text = str(detail or "request rejected")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1[REDACTED]", text)
    return text[:500]


def _response_detail(response: requests.Response, api_key: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "request rejected by provider"

    if isinstance(payload, dict):
        detail = (
            payload.get("Error Message")
            or payload.get("error")
            or payload.get("message")
        )
        if isinstance(detail, dict):
            detail = detail.get("message") or detail
        return _redact_detail(detail, api_key)
    if isinstance(payload, str):
        return _redact_detail(payload, api_key)
    return "request rejected by provider"


def get_fmp_data(
    endpoint: str,
    api_key: str,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    """Fetch JSON from an FMP Stable endpoint without leaking credentials."""
    if not api_key:
        raise ValueError("FMP API key is required")

    endpoint = endpoint.strip("/")
    url = f"{FMP_STABLE_BASE_URL}/{endpoint}"
    request_params = dict(params or {})
    request_params["apikey"] = api_key

    response = None
    network_error_status = None
    try:
        response = requests.get(url, params=request_params, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        # Do not chain Requests' exception: it may contain the fully prepared URL,
        # including the API key. Raise after leaving the except block so neither
        # __cause__ nor __context__ retains the provider exception.
        network_error_status = getattr(
            getattr(exc, "response", None), "status_code", None
        )

    if response is None:
        raise FMPAPIError(endpoint, network_error_status, "network request failed")

    if not response.ok:
        raise FMPAPIError(
            endpoint,
            response.status_code,
            _response_detail(response, api_key),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise FMPAPIError(endpoint, response.status_code, "provider returned invalid JSON") from exc

    if isinstance(payload, dict):
        provider_error = payload.get("Error Message") or payload.get("error")
        if provider_error:
            raise FMPAPIError(
                endpoint,
                response.status_code,
                _redact_detail(provider_error, api_key),
            )
    elif isinstance(payload, str):
        raise FMPAPIError(
            endpoint,
            response.status_code,
            _redact_detail(payload, api_key),
        )

    return payload
