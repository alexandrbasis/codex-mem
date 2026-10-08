"""Process-wide bounds for live Jev HTTP calls.

Eligibility, quality and other callers share four permits in this interpreter.
Separate CLI processes have independent bounds; this is not a cross-process or
provider rate limit. Acquire only at the HTTP boundary, never around a batch.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import BoundedSemaphore
import time
from collections.abc import Iterator


MAX_JEV_IN_FLIGHT = 4
_jev_slots = BoundedSemaphore(MAX_JEV_IN_FLIGHT)


class JevSlotTimeout(TimeoutError):
    """A fixed, content-free deadline error while waiting for a permit."""

    def __init__(self) -> None:
        super().__init__("jev_slot_timeout")


@contextmanager
def acquire_jev_slot(deadline: float) -> Iterator[None]:
    """Hold one HTTP permit until response reading or failure finishes.

    Waiting uses the caller's absolute monotonic deadline. A permit obtained
    at the deadline is released without allowing a late dispatch.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _jev_slots.acquire(timeout=remaining):
        raise JevSlotTimeout()
    try:
        if time.monotonic() >= deadline:
            raise JevSlotTimeout()
        yield
    finally:
        _jev_slots.release()
