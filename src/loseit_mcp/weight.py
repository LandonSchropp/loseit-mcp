"""Weight recording via the ``saveRecordedWeight`` GWT-RPC method.

Not covered by the upstream SDK, so the envelope is built here. Captured from
the web app's "today's weight" widget:

    7|0|10|<base>|<policy>|<service>|saveRecordedWeight
      |<ServiceRequestToken>|D|<DayDate>|<UserId>|<user_name>|<Date>
      |1|2|3|4|3|5|6|7|5|0|8|<user_id>|9|<tz>|<weight>|7|10|<day_key>|<day_num>|<tz>|

The three arguments are the request token, a primitive ``double`` (the weight,
in the account's display unit), and the ``DayDate`` to record it against.

The UI also calls ``isRecordedWeightSuspect`` first, which is a client-side
sanity prompt for implausible jumps rather than a precondition for saving, so
it is not replicated here.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import lose_it.core.daily as _daily
from lose_it.core._config import Config
from lose_it.core._dates import day_number_for
from lose_it.core._decoder import decode_response
from lose_it.core._gwt import build_envelope, fmt_num, parse_response
from lose_it.core._http import HttpClient, LoseItAuthError, LoseItError
from lose_it.core.daily import get_daydate_key

_SERVICE = "com.loseit.core.client.service.LoseItRemoteService"
_REQUEST_TOKEN = "com.loseit.core.client.service.ServiceRequestToken/1076571655"
_DAY_DATE = "com.loseit.core.shared.model.DayDate/1611136587"
_USER_ID = "com.loseit.core.client.model.UserId/4281239478"
_DATE = "java.util.Date/3385151746"

_RECORDED_WEIGHT = "com.loseit.core.client.model.RecordedWeight/1309383152"

# The server accepts this placeholder wherever a day_key is expected but not
# cached; it only really keys off day_num.
_FALLBACK_DAY_KEY = "ZZZZZZZ"

# Range responses grow with the number of logged entries, and past roughly
# 200 KB the SDK's decoder degrades to a partial result. 60 days keeps us well
# inside that limit for a heavily-logged account.
MAX_RANGE_DAYS = 60

# Bisection bound, so a pathological response can't fan out unboundedly.
MAX_BISECT_DEPTH = 7


_GATEWAY = "https://gateway.loseit.com"

# The sync transaction version the Android app sends.
_TRANSACTION_VERSION = 2


class WeightHistoryError(RuntimeError):
    """The weight history could not be read reliably."""


def _build_payload(config: Config, weight: float, day_key: str, day_num: int) -> str:
    strings = [
        config.base_url,  # 1
        config.policy_hash,  # 2
        _SERVICE,  # 3
        "saveRecordedWeight",  # 4
        _REQUEST_TOKEN,  # 5
        "D",  # 6 — primitive double
        _DAY_DATE,  # 7
        _USER_ID,  # 8
        config.user_name,  # 9
        _DATE,  # 10
    ]
    tz = str(config.hours_from_gmt)
    data = [
        "1",
        "2",
        "3",
        "4",
        "3",  # three arguments follow
        "5",  # arg 1 type: ServiceRequestToken
        "6",  # arg 2 type: double
        "7",  # arg 3 type: DayDate
        # arg 1 value
        "5",
        "0",
        "8",
        str(config.user_id),
        "9",
        tz,
        # arg 2 value
        fmt_num(weight),
        # arg 3 value
        "7",
        "10",
        day_key,
        str(day_num),
        tz,
    ]
    return build_envelope(strings, data)


def save_weight(http: HttpClient, weight: float, when: date) -> float:
    """Record ``weight`` for ``when``; returns the weight the server echoes back.

    The weight is interpreted in whatever unit the account displays (lb or kg);
    the API carries no unit of its own.
    """
    day_num = day_number_for(when)
    day_key = get_daydate_key(http, day_num) or ""
    payload = _build_payload(http.config, weight, day_key, day_num)
    response = http.post_rpc(payload)

    # The response leads with the saved value, but the surrounding slots are
    # response-shape-specific and a bare positional scan can pick up an
    # unrelated number. Only trust the echo when it corroborates what we sent;
    # a confidently wrong confirmation is worse than none.
    tokens, _ = parse_response(response)
    for token in tokens[:3]:
        if isinstance(token, int | float) and token > 0 and abs(float(token) - weight) <= 0.5:
            return float(token)
    return weight


def _key(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _varint_field(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _double_field(field: int, value: float) -> bytes:
    return _key(field, 1) + struct.pack("<d", value)


def _message_field(field: int, body: bytes) -> bytes:
    return _key(field, 2) + _varint(len(body)) + body


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _read_fields(data: bytes) -> Iterator[tuple[int, Any]]:
    """Yield ``(field, value)`` pairs; doubles decode, messages stay raw bytes."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
        elif wire_type == 1:
            value, pos = struct.unpack_from("<d", data, pos)[0], pos + 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        else:  # 5: fixed32
            value, pos = int.from_bytes(data[pos : pos + 4], "little"), pos + 4
        yield field, value


def _gateway_post(http: HttpClient, path: str, body: bytes) -> bytes:
    """POST to the mobile sync gateway."""
    response = httpx.post(
        _GATEWAY + path,
        headers={
            "authorization": f"Bearer {http.token}",
            "content-type": "application/octet-stream; charset=utf-8",
            "x-loseit-device": f"DROID-UID-{_device_id(http).hex.upper()}",
            "x-loseit-device-type": "Android",
            "x-loseit-hoursfromgmt": str(http.config.hours_from_gmt),
            "cookie": "loseitlocale=en-US",
        },
        content=body,
        timeout=120.0,
    )
    if response.status_code in (401, 403):
        raise LoseItAuthError(f"Lose It's sync gateway returned {response.status_code}")
    if response.status_code != 200:
        raise LoseItError(f"Lose It's sync gateway returned {response.status_code}")
    return response.content


def _device_id(http: HttpClient) -> uuid.UUID:
    """One stable device per account, as a phone install would be."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"loseit-mcp:{http.config.user_id}")


def _transaction(payload: bytes) -> tuple[int, bytes]:
    txn_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
    body = _varint_field(1, txn_id) + payload + _varint_field(16, _TRANSACTION_VERSION)
    return txn_id, body


def delete_weight(http: HttpClient, when: date) -> float:
    """Delete the weigh-in recorded for ``when``; returns the weight removed.

    Every day from ``when`` up to the next weigh-in goes back to the weight,
    calorie burn and budget of the last logged day before ``when``. Deleting the
    latest weigh-in also sets the account's current weight to the one before it.

    Raises ``ValueError`` if ``when`` has no weigh-in, and ``LoseItError`` if
    Lose It does not confirm the deletion.
    """
    day_num = day_number_for(when)
    database = sqlite3.connect(":memory:")
    database.deserialize(_gateway_post(http, "/user/database?newschema", b""))

    row = database.execute(
        "SELECT Weight FROM RecordedWeights WHERE Date = ? AND Deleted = 0", (day_num,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No weigh-in is recorded for {when.isoformat()}.")
    weight = row[0]

    now = int(time.time() * 1000)
    recorded = (
        _varint_field(1, day_num)
        + _double_field(2, weight)
        + _varint_field(4, now)
        + _varint_field(5, 1)
    )
    transactions = [_transaction(_message_field(6, recorded))]

    (next_weigh_in,) = database.execute(
        "SELECT MIN(Date) FROM RecordedWeights WHERE Date > ? AND Deleted = 0", (day_num,)
    ).fetchone()

    previous = database.execute(
        "SELECT CurrentWeight, CurrentEER, CurrentActivityLevel, BudgetCalories "
        "FROM DailyLogEntries WHERE Date < ? ORDER BY Date DESC LIMIT 1",
        (day_num,),
    ).fetchone()
    if previous is not None:
        prior_weight, eer, activity_level, budget = previous
        days = database.execute(
            "SELECT Date, FoodCalories, ExerciseCalories FROM DailyLogEntries "
            "WHERE Date >= ? AND Date < ? ORDER BY Date",
            (day_num, next_weigh_in or 2**31),
        ).fetchall()
        burn = (
            _double_field(1, prior_weight)
            + _double_field(2, eer)
            + _varint_field(3, activity_level)
        )
        goals = _double_field(1, budget) + _message_field(2, burn)
        if days:
            restored = b"".join(
                _message_field(
                    12,
                    _varint_field(1, day)
                    + _message_field(2, goals)
                    + _double_field(3, food or 0.0)
                    + _double_field(4, exercise or 0.0)
                    + _varint_field(5, now),
                )
                for day, food, exercise in days
            )
            transactions.append(_transaction(restored))

    # Restore Goals.currentWeight when the latest weigh-in is deleted.
    if next_weigh_in is None and (
        earlier := database.execute(
            "SELECT Weight FROM RecordedWeights WHERE Date < ? AND Deleted = 0 "
            "ORDER BY Date DESC LIMIT 1",
            (day_num,),
        ).fetchone()
    ):
        pair = _message_field(1, b"currentWeight") + _message_field(2, str(earlier[0]).encode())
        bag = _message_field(1, b"Goals") + _message_field(2, pair) + _varint_field(3, now)
        transactions.append(_transaction(_message_field(11, bag)))

    # One request per transaction: in a shared bundle the server re-derives the
    # deleted day's row from the weigh-in after applying ours.
    for txn_id, body in transactions:
        bundle = _message_field(1, body) + _varint_field(2, now)
        bundle += _varint_field(4, int(http.config.user_id))
        response = _gateway_post(http, "/user/loseItTransactionBundle", bundle)
        if txn_id not in {v for f, v in _read_fields(response) if f == 1}:
            raise LoseItError("Lose It did not confirm the weigh-in deletion.")
    return weight


def _walk(node: Any, out: list[dict[str, Any]]) -> None:
    """Collect every RecordedWeight object nested anywhere in a decoded tree."""
    if isinstance(node, dict):
        if node.get("__type__") == _RECORDED_WEIGHT:
            out.append(node)
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out)


def _epoch_ms_to_iso(millis: Any) -> str | None:
    if not isinstance(millis, int | float) or millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()


def _fetch_window(
    http: HttpClient,
    start_num: int,
    end_num: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch one range window; returns (RecordedWeight nodes, was_partial)."""
    payload = _daily.build_range_payload(
        http.config,
        start_num,
        _FALLBACK_DAY_KEY,
        end_num,
        _FALLBACK_DAY_KEY,
    )
    decoded = decode_response(http.post_rpc(payload))
    partial = bool(isinstance(decoded, dict) and decoded.get("__partial__"))
    found: list[dict[str, Any]] = []
    _walk(decoded, found)
    return found, partial


def _collect_window(
    http: HttpClient,
    window_start: int,
    window_end: int,
    into: dict[int, dict[str, Any]],
    origin: date,
    origin_num: int,
    depth: int = 0,
) -> None:
    """Fetch one window, bisecting when the response is too large to decode.

    Response size scales with how much was logged, not with the number of days,
    so no fixed window is safe for every account or period. On a partial decode
    we halve the window and retry rather than guessing — and rather than
    returning the empty result an undetected partial decode would produce.
    """
    found, partial = _fetch_window(http, window_start, window_end)

    if partial:
        if window_start >= window_end:
            raise WeightHistoryError(
                "A single day's Lose It! response was too large to decode "
                f"({_num_to_date(origin, origin_num, window_start).isoformat()})."
            )
        if depth >= MAX_BISECT_DEPTH:
            raise WeightHistoryError(
                "Could not decode the Lose It! weight history even after "
                f"{depth} range subdivisions. Try a shorter date range."
            )
        midpoint = window_start + (window_end - window_start) // 2
        _collect_window(http, window_start, midpoint, into, origin, origin_num, depth + 1)
        _collect_window(http, midpoint + 1, window_end, into, origin, origin_num, depth + 1)
        return

    for node in found:
        day_date = node.get("f0") or {}
        day_num = day_date.get("f1") if isinstance(day_date, dict) else None
        weight = node.get("f3")
        if not isinstance(day_num, int) or not isinstance(weight, int | float):
            continue
        # Anchor weights (starting weight, goal baselines) ride along outside
        # the requested window; drop anything we didn't ask for.
        if not (window_start <= day_num <= window_end):
            continue
        into[day_num] = {
            "date": _num_to_date(origin, origin_num, day_num).isoformat(),
            "weight": round(float(weight), 2),
            "recorded_at": _epoch_ms_to_iso(node.get("f2")),
        }


def get_weight_history(
    http: HttpClient,
    start: date,
    end: date,
    *,
    chunk_days: int = MAX_RANGE_DAYS,
) -> list[dict[str, Any]]:
    """Return recorded weigh-ins for the inclusive range ``[start, end]``.

    Read-only: this reuses ``getDailyDetailsIncludingPendingForDateRange``,
    whose payload embeds a ``RecordedWeight`` per day, rather than
    ``saveRecordedWeight`` (which would write).

    The request is split into windows of at most ``chunk_days``, and any window
    whose response is still too large is bisected further. A single wide range
    overflows the SDK decoder and degrades to a partial decode in which the only
    surviving ``RecordedWeight`` nodes are out-of-range anchors — so an
    unchunked year-long query would otherwise return "no weigh-ins" rather than
    failing.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")

    start_num = day_number_for(start)
    end_num = day_number_for(end)
    span = max(1, min(chunk_days, MAX_RANGE_DAYS))

    by_day: dict[int, dict[str, Any]] = {}
    window_start = start_num
    while window_start <= end_num:
        window_end = min(window_start + span - 1, end_num)
        _collect_window(http, window_start, window_end, by_day, start, start_num)
        window_start = window_end + 1

    return [by_day[k] for k in sorted(by_day)]


def _num_to_date(start: date, start_num: int, day_num: int) -> date:
    """Map a Lose It day number back to a calendar date.

    Both directions are plain proleptic-ordinal arithmetic (``day_number_for``
    is ``anchor + (d - anchor).days``), so this is timezone- and DST-immune.
    """
    return start + timedelta(days=day_num - start_num)
