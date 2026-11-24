"""S2 (viewer speed plan): short-lived memo of DICOMweb read authorisation.

A viewer opening a 683-slice CT sends one ``/frames/N`` request per slice, and
the router guard used to answer "who is this?" (session + user rows) and "may
they see this study?" (study row) from the database for every one of them —
about 8 ms of a 27 ms frame, on a single-process server.

This memo keeps only **successful** answers, for ``TTL_SECONDS``:

* identity: key = SHA-256 of the presented bearer token / session cookie
  (the raw token is never stored), value = the detached ``User`` row;
* study access: key = (user id, tenant scope, study UID), value = True.

Safety:

* failures (401/404) are never memoised, so a denial is always re-checked;
* any flush or bulk statement that touches ``Session``, ``User`` or ``Study``
  rows in this process clears the whole memo at once — logout, admin revoke,
  password reset, deactivation, role/tenant change, study re-assignment;
* a change made by **another** process (multi-worker deployment) is picked up
  within ``TTL_SECONDS`` at most;
* share-token access is not memoised (rare, and scoped by its own table).
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession

TTL_SECONDS = 5.0
MAX_ENTRIES = 4096

_identities: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_study_access: OrderedDict[tuple, float] = OrderedDict()
_clock = time.monotonic


def token_key(kind: str, token: str) -> str:
    return kind + ":" + hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _evict(store: OrderedDict) -> None:
    while len(store) > MAX_ENTRIES:
        store.popitem(last=False)


def get_identity(key: str) -> Any | None:
    entry = _identities.get(key)
    if entry is None:
        return None
    stored_at, user = entry
    if _clock() - stored_at > TTL_SECONDS:
        _identities.pop(key, None)
        return None
    return user


def put_identity(key: str, user: Any) -> None:
    _identities[key] = (_clock(), user)
    _identities.move_to_end(key)
    _evict(_identities)


def has_study_access(key: tuple) -> bool:
    stored_at = _study_access.get(key)
    if stored_at is None:
        return False
    if _clock() - stored_at > TTL_SECONDS:
        _study_access.pop(key, None)
        return False
    return True


def put_study_access(key: tuple) -> None:
    _study_access[key] = _clock()
    _study_access.move_to_end(key)
    _evict(_study_access)


def clear() -> None:
    _identities.clear()
    _study_access.clear()


# --------------------------------------------------------------------------- #
# Invalidation: any write to the rows these answers came from clears the memo.
# --------------------------------------------------------------------------- #
def _watched_classes() -> tuple[type, ...]:
    from app.db.models import Session, Study, User

    return (Session, User, Study)


@event.listens_for(OrmSession, "after_flush")
def _clear_on_flush(session: OrmSession, _flush_context) -> None:
    watched = _watched_classes()
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, watched):
            clear()
            return


@event.listens_for(OrmSession, "do_orm_execute")
def _clear_on_bulk(orm_execute_state) -> None:
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    mapper = orm_execute_state.bind_mapper
    if mapper is not None and issubclass(mapper.class_, _watched_classes()):
        clear()
