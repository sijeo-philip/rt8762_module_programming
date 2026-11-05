"""The station's connection to the UnisemBLE box (docs/box_integration.md).

The only module that holds the SDK client (unisemble_client 7.0). One
client serves the whole application for its lifetime: it carries the
station key and, while someone is logged in, that person's box session.

The SDK runs one call at a time and refuses a second with ClientBusy.
Interactive calls (login, blocks, clearing a hold) wait here for their
turn; a background report does not wait, it keeps its modules for the
next attempt.

The box holds one person per station: a login ends whoever was logged
in before, and a report is recorded against the person logged in.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from collections.abc import Callable, Sequence
from typing import TypeVar

from unisemble_client import Block, Module, ReportResult, UnisembleClient, User, errors

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Longest a retried SDK call can hold the client (about 20 s), plus margin.
WAIT_SECONDS = 30.0


def box_url_problem(box_url: str) -> str | None:
    """Why the SDK would refuse box_url at once, or None."""
    try:
        parts = urllib.parse.urlsplit(box_url)
        parts.port  # ValueError for a port that is not a number
    except ValueError:
        return f"BOX_URL {box_url!r} is not a valid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"BOX_URL {box_url!r} must look like http://<box-host>:<port>"
    return None


class BoxGateway:
    def __init__(self, *, box_url: str, station_key: str, transport=None) -> None:
        self._url = box_url
        self._client = UnisembleClient(box_url, station_key, transport=transport)
        self._lock = threading.Lock()
        self._holder: User | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def holder(self) -> User | None:
        """Who holds the box session on this station, as far as we know."""
        return self._holder

    # -- people ----------------------------------------------------------

    def login(self, phone: str, pin: str) -> User:
        """Raises PinChangeRequired, WrongPin, AccountLocked, Refused."""
        self._holder = None  # the SDK logs out the previous person first
        user = self._call(lambda: self._client.login(phone, pin))
        self._holder = user
        return user

    def change_pin(self, current_pin: str, new_pin: str) -> User | None:
        """After PinChangeRequired this logs the person in and returns them."""
        user = self._call(lambda: self._client.change_pin(current_pin, new_pin))
        if user is not None:
            self._holder = user
        return user

    def logout(self) -> None:
        self._holder = None
        try:
            self._call(self._client.logout)
        except errors.UnisembleError as exc:
            # The SDK forgets the session whatever the answer.
            logger.warning("Box logout not confirmed: %s", exc)

    def clear_hold(self, phone: str, pin: str) -> bool:
        return self._call(lambda: self._client.clear_hold(phone, pin))

    # -- addresses -------------------------------------------------------

    def request_block(self, size: int, master_range: str) -> Block:
        return self._call(lambda: self._client.request_block(size, master_range))

    def promote_block(self, old_block_id: int, new_block_id: int) -> None:
        self._call(lambda: self._client.promote_block(old_block_id, new_block_id))

    # -- the record ------------------------------------------------------

    def report(self, modules: Sequence[Module], *, wait: bool = False) -> ReportResult:
        try:
            return self._call(lambda: self._client.report(list(modules)), wait=wait)
        except (errors.SessionExpired, errors.NotLoggedIn):
            self._holder = None
            raise

    # ------------------------------------------------------------------

    def _call(self, call: Callable[[], T], *, wait: bool = True) -> T:
        acquired = self._lock.acquire(timeout=WAIT_SECONDS) if wait else self._lock.acquire(blocking=False)
        if not acquired:
            raise errors.ClientBusy("Another call to the box is still in progress")
        try:
            return call()
        finally:
            self._lock.release()
