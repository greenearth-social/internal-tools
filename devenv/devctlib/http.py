"""Small HTTP requests: health probes and Elasticsearch queries.

Status 0 means nothing answered (what curl reports as 000): refused, timed
out, or unresolvable.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass
class Response:
    status: int
    body: str = ""

    @property
    def code(self) -> str:
        """The status as curl's %{http_code} prints it ("000" for none)."""
        return f"{self.status:03d}"


def request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
    timeout: float = 10,
    insecure: bool = False,
) -> Response:
    """One request; an HTTP error status is a Response, not an exception."""
    req = urllib.request.Request(
        url,
        data=body.encode() if body is not None else None,
        headers=dict(headers or {}),
        method=method,
    )
    context = None
    if insecure and url.startswith("https:"):
        context = ssl._create_unverified_context()  # noqa: S323 - tunnels to self-signed ES
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return Response(resp.status, resp.read().decode(errors="replace"))
    except urllib.error.HTTPError as err:
        return Response(err.code, err.read().decode(errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return Response(0)


def status(url: str, **kwargs) -> Response:
    """Just whether and how a URL answers."""
    return request(url, **kwargs)
