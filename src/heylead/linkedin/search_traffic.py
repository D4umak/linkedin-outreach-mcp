"""Counting the search requests that actually leave this machine.

A collector's daily search budget exists to keep one LinkedIn account under
its search allowance. It can only do that if a unit is booked for the
requests LinkedIn saw, and for nothing else.

The collectors cannot tell on their own. Both search clients swallow their
own transport errors and return ``([], None)``, so one level up a DNS failure
and a search with no results are the same value. Booking a unit per call
therefore charged a whole day's allowance to an outage that lasted seconds:
one 30-minute tick of connect failures filled all three collectors' counters
and the remaining 47 ticks of the day issued nothing at all — a transient,
self-healing condition turned into a silent 24-hour outage of the signal
pipeline.

So the client counts, because the client is the only layer that knows.
``SearchTraffic.send`` awaits the HTTP call and counts it exactly once: on
any response, whatever its status (the request was sent and answered), and
on any error except the ones that prove nothing was ever written to a
socket. Unrecognised failures count — an unclassified error may well have
reached LinkedIn, and under-counting is the direction that gets an account
restricted, which is what the cap is for in the first place.

The count a collector reads is *per call*, not per client, and that is not a
detail. In backend/hosted mode ``get_linkedin_client()`` hands every caller
the same ``BackendClient``, and the scheduler runs up to five jobs at once
(``asyncio.Semaphore(5)`` + ``asyncio.gather``), several of them searching.
A collector reading one shared attribute either side of its own ``await``
would be asking "did any search anywhere finish while I was suspended",
which a busy neighbour answers yes to — so a dead request would book a unit
after all, in the deployment mode the setup flow offers first. Instead
:func:`search_scope` puts a fresh tally in a :class:`~contextvars.ContextVar`
for the duration of one call. Each scheduler job is its own ``asyncio.Task``
and therefore has its own copy of the context, so neighbours cannot reach
this tally; work the call itself spawns inherits the same tally object by
reference, so its requests still land in the right place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx

# Failures raised before any byte of the request is written. Everything else
# — read timeouts, write timeouts, protocol errors, HTTP status errors, and
# anything unrecognised — counts as sent.
NEVER_SENT: tuple[type[BaseException], ...] = (
    httpx.ConnectError,          # refused, unreachable, DNS or TLS failure
    httpx.ConnectTimeout,
    httpx.PoolTimeout,           # no connection was ever handed out
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
    ConnectionError,             # builtin: refused/reset before the send, and
                                 # what a non-httpx transport raises
    ImportError,                 # the call site never ran at all
)


class SearchScope:
    """What one search call put on the wire, isolated from every other call.

    A mutable object rather than a plain integer on purpose: contexts are
    *copied* when a task is created, so a task started inside this scope
    inherits this same instance by reference and its increments are visible
    here (a per-task integer would lose them). A task started outside it —
    every scheduler job, since they are gathered from a context with no
    scope in it — carries a different value and cannot touch this one.
    """

    __slots__ = ("client_counts", "issued")

    def __init__(self, *, client_counts: bool) -> None:
        self.issued = 0
        # Whether the client this scope was opened for keeps a SearchTraffic
        # at all. A client that does not (a test double, a future client) can
        # only be judged by the exception it raised.
        self.client_counts = client_counts

    def request_was_issued(self, exc: BaseException | None) -> bool:
        """Did at least one request leave this machine for that search?"""
        if self.client_counts:
            return self.issued > 0
        return exc is None or not isinstance(exc, NEVER_SENT)


_SCOPE: ContextVar[SearchScope | None] = ContextVar(
    "heylead_search_scope", default=None,
)


@contextmanager
def search_scope(client: object) -> Iterator[SearchScope]:
    """Tally what is issued inside this block, and nothing issued outside it."""
    scope = SearchScope(client_counts=issued_count(client) is not None)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


class SearchTraffic:
    """How many search requests this client has put on the wire.

    ``issued`` is a whole-process running total for this client: in
    backend/hosted mode one client is shared by every caller, so it is the
    sum of everybody's traffic and no single caller may read it as its own.
    Collectors use :func:`search_scope` for that. What ``issued`` is for is
    telling a client that counts from one that does not, and reporting the
    client's total traffic. In-memory and monotonic — never persisted, never
    reset.
    """

    __slots__ = ("issued",)

    def __init__(self) -> None:
        self.issued = 0

    async def send(self, request: Awaitable[Any]) -> Any:
        """Await one search request, counting it iff it went out.

        Counting here rather than at the call site is what makes it exactly
        once: a response that then fails to parse, or a 4xx that becomes an
        exception further down, is still one request and must not be booked
        twice.
        """
        try:
            response = await request
        except BaseException as exc:
            if not isinstance(exc, NEVER_SENT):
                self._count()
            raise
        self._count()
        return response

    def _count(self) -> None:
        self.issued += 1
        scope = _SCOPE.get()
        if scope is not None:
            scope.issued += 1


def issued_count(client: object) -> int | None:
    """This client's issued-request counter, or None if it keeps none."""
    traffic = getattr(client, "search_traffic", None)
    issued = getattr(traffic, "issued", None)
    return issued if isinstance(issued, int) else None
