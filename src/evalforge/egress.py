"""Egress guard for tier-0 scorers.

The Goldfish were blunt: handing a scorer a context object without a provider client is
capability *hiding*, not enforcement — the scorer can ``import httpx`` and open its own
connection. This module enforces at the socket instead.

    with blocked():
        run_free_scorer(item)      # any network attempt raises EgressViolation

Honest limits, stated in the README too:
  * This stops accident and casual misuse. It is **not** a sandbox against hostile code.
  * A scorer could re-import a fresh socket module from disk; defeating that needs a
    subprocess with no network namespace, which is v0.2.
  * It patches the stdlib entry points that all common HTTP clients funnel through.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
from typing import Iterator


class EgressViolation(RuntimeError):
    """Raised when a tier-0 scorer attempts network access."""


def _blocked(*_args, **_kwargs):
    raise EgressViolation(
        "tier-0 scorers must not use the network; declare the scorer as tier 1 and "
        "take a provider client from the context instead"
    )


@contextlib.contextmanager
def blocked() -> Iterator[None]:
    """Deny outbound network for the duration of the block, then restore."""
    saved = {
        "socket": socket.socket,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
        "wrap_socket": ssl.SSLContext.wrap_socket,
    }
    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]
    ssl.SSLContext.wrap_socket = _blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = saved["socket"]  # type: ignore[assignment]
        socket.create_connection = saved["create_connection"]  # type: ignore[assignment]
        socket.getaddrinfo = saved["getaddrinfo"]  # type: ignore[assignment]
        ssl.SSLContext.wrap_socket = saved["wrap_socket"]  # type: ignore[assignment]
