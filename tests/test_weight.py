"""Weight recording and history.

The history regression is the important one: an oversize response degraded to a
partial decode whose only surviving records were out-of-range anchors, so a
year-long query confidently answered "no weigh-ins".
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import httpx
import pytest
from lose_it.core._dates import day_number_for
from lose_it.core._http import LoseItAuthError, LoseItError

from loseit_mcp import weight as W
from loseit_mcp.weight import (
    MAX_RANGE_DAYS,
    WeightHistoryError,
    _build_payload,
    _num_to_date,
    delete_weight,
    get_weight_history,
    save_weight,
)


class FakeConfig:
    base_url = "https://example.invalid/web/"
    policy_hash = "POLICY"
    user_id = "10313492"
    user_name = "Tester"
    hours_from_gmt = -7


def _recorded_weight(day_num: int, value: float) -> dict[str, Any]:
    return {
        "__type__": "com.loseit.core.client.model.RecordedWeight/1309383152",
        "f0": {
            "__type__": "com.loseit.core.shared.model.DayDate/1611136587",
            "f1": day_num,
        },
        "f1": False,
        "f2": 1785288782000,
        "f3": value,
    }


class TestDayNumberMapping:
    """The mapping is pure ordinal arithmetic, so it must be DST- and
    timezone-immune."""

    @pytest.mark.parametrize(
        "anchor",
        [
            date(2025, 3, 8),   # US DST spring-forward
            date(2025, 11, 1),  # US DST fall-back
            date(2024, 2, 29),  # leap day
            date(2026, 7, 28),
        ],
    )
    def test_round_trips_across_dst_and_leap_boundaries(self, anchor: date) -> None:
        start_num = day_number_for(anchor)
        for offset in range(-400, 401):
            target = anchor + timedelta(days=offset)
            assert _num_to_date(anchor, start_num, day_number_for(target)) == target


class TestSaveWeight:
    def test_payload_carries_method_and_values(self) -> None:
        payload = _build_payload(FakeConfig(), 199.2, "DAYKEY", 9340)
        assert "saveRecordedWeight" in payload
        assert "199.2" in payload
        assert "DAYKEY" in payload
        assert payload.startswith("7|0|10|")

    def test_integer_weights_are_not_rendered_as_floats(self) -> None:
        assert "|200|" in _build_payload(FakeConfig(), 200.0, "K", 1)

    def test_echo_is_trusted_only_when_it_corroborates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the echo scanned for the first positive number in the
        response preamble, which is not a stable slot across RPCs."""
        monkeypatch.setattr(W, "get_daydate_key", lambda *a, **k: "K")

        class Http:
            config = FakeConfig()

            def __init__(self, body: str) -> None:
                self._body = body

            def post_rpc(self, payload: str) -> str:
                return self._body

        # Server echoes the value we sent -> trusted.
        assert save_weight(Http('//OK[0,199.2,"X",1,["a"],0,7]'), 199.2, date(2026, 7, 28)) == 199.2

        # Response leads with an unrelated quantity -> fall back to our input
        # rather than reporting a confidently wrong confirmation.
        assert save_weight(Http('//OK[0,170.4,2425.9,1,["a"],0,7]'), 199.2, date(2026, 7, 28)) == 199.2


class TestWeightHistory:
    def _install(
        self, monkeypatch: pytest.MonkeyPatch, windows: list[tuple[list[dict], bool]]
    ) -> list[tuple[int, int]]:
        """Record which day-number windows get requested."""
        seen: list[tuple[int, int]] = []
        queue = list(windows)

        def fake_fetch(http: Any, start_num: int, end_num: int) -> tuple[list[dict], bool]:
            seen.append((start_num, end_num))
            return queue.pop(0) if queue else ([], False)

        monkeypatch.setattr(W, "_fetch_window", fake_fetch)
        return seen

    def test_rejects_backwards_range(self) -> None:
        with pytest.raises(ValueError):
            get_weight_history(None, date(2026, 7, 28), date(2026, 7, 1))

    def test_filters_out_of_range_anchor_weights(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The response carries the account's starting weight from years ago."""
        start, end = date(2026, 7, 21), date(2026, 7, 23)
        s = day_number_for(start)
        self._install(
            monkeypatch,
            [([_recorded_weight(s, 201.0), _recorded_weight(4777, 235.0)], False)],
        )
        out = get_weight_history(None, start, end)
        assert [e["weight"] for e in out] == [201.0]

    def test_long_ranges_are_chunked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = date(2025, 7, 28)
        end = date(2026, 7, 28)
        seen = self._install(monkeypatch, [])
        get_weight_history(None, start, end)

        assert len(seen) > 1, "a year must not be requested as one window"
        assert all(e - s + 1 <= MAX_RANGE_DAYS for s, e in seen)
        # Contiguous and complete.
        assert seen[0][0] == day_number_for(start)
        assert seen[-1][1] == day_number_for(end)
        for (_, prev_end), (next_start, _) in pairwise(seen):
            assert next_start == prev_end + 1

    def test_partial_decode_bisects_rather_than_returning_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: a partial decode silently produced zero weigh-ins."""
        start, end = date(2026, 6, 1), date(2026, 6, 4)
        s = day_number_for(start)
        # First call reports partial; the two halves then succeed.
        self._install(
            monkeypatch,
            [
                ([], True),
                ([_recorded_weight(s, 200.0)], False),
                ([_recorded_weight(s + 2, 199.0)], False),
            ],
        )
        out = get_weight_history(None, start, end)
        assert [e["weight"] for e in out] == [200.0, 199.0]

    def test_single_day_partial_decode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = date(2026, 6, 1)
        self._install(monkeypatch, [([], True)])
        with pytest.raises(WeightHistoryError):
            get_weight_history(None, day, day)

    def test_results_are_sorted_and_deduplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start, end = date(2026, 6, 1), date(2026, 6, 3)
        s = day_number_for(start)
        self._install(
            monkeypatch,
            [([_recorded_weight(s + 2, 3.0), _recorded_weight(s, 1.0), _recorded_weight(s, 1.0)], False)],
        )
        out = get_weight_history(None, start, end)
        assert [e["date"] for e in out] == ["2026-06-01", "2026-06-03"]

    def test_malformed_nodes_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start = date(2026, 6, 1)
        s = day_number_for(start)
        bad = {"__type__": W._RECORDED_WEIGHT, "f0": None, "f3": "not-a-number"}
        self._install(monkeypatch, [([bad, _recorded_weight(s, 1.0)], False)])
        assert len(get_weight_history(None, start, start)) == 1


def _database(weights: list[tuple[int, float]], days: list[tuple]) -> bytes:
    """A gateway database snapshot holding only the tables a delete reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE RecordedWeights (Date, Weight, LastUpdated, Deleted)")
    conn.execute(
        "CREATE TABLE DailyLogEntries (Date, CurrentWeight, CurrentEER, CurrentActivityLevel, "
        "BudgetCalories, FoodCalories, ExerciseCalories, LastUpdated)"
    )
    conn.executemany("INSERT INTO RecordedWeights VALUES (?, ?, 0, 0)", weights)
    conn.executemany("INSERT INTO DailyLogEntries VALUES (?, ?, ?, ?, ?, ?, ?, 0)", days)
    return conn.serialize()


def _decode(data: bytes) -> list[tuple[int, Any]]:
    return list(W._read_fields(data))


class TestDeleteWeight:
    class Http:
        config = FakeConfig()

    DAY = date(2026, 10, 14)
    NUM = day_number_for(DAY)

    def _install(self, monkeypatch: pytest.MonkeyPatch, database: bytes, ack: bool = True) -> list:
        sent: list[bytes] = []

        def fake_post(http: Any, path: str, body: bytes) -> bytes:
            if path.startswith("/user/database"):
                return database
            sent.append(body)
            ids = [v for f, t in _decode(body) if f == 1 for k, v in _decode(t) if k == 1]
            return b"".join(W._varint_field(1, i) for i in ids) if ack else b""

        monkeypatch.setattr(W, "_gateway_post", fake_post)
        return sent

    def test_marks_the_weigh_in_deleted_and_restores_the_days_until_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self.NUM
        database = _database(
            [(n - 8, 230.2), (n, 150.0), (n + 3, 228.0)],
            [
                (n - 1, 230.2, 2405.0, 2, 1905.0, 1142.0, 186.0),
                (n, 150.0, 1968.9, 2, 1968.9, 500.0, 375.0),
                (n + 1, 150.0, 1968.9, 2, 1968.9, 0.0, 0.0),
                (n + 3, 228.0, 2400.0, 2, 1900.0, 0.0, 0.0),
            ],
        )
        sent = self._install(monkeypatch, database)

        assert delete_weight(self.Http(), self.DAY) == 150.0

        # The weigh-in goes first, and on its own: sharing a request lets the
        # server overwrite the restored day with the deleted weight.
        assert len(sent) == 2
        transactions = [t for body in sent for f, t in _decode(body) if f == 1]
        assert [f for f, _ in _decode(transactions[0])] == [1, 6, 16]
        weights = [dict(_decode(v)) for t in transactions for f, v in _decode(t) if f == 6]
        assert len(weights) == 1
        assert weights[0][1] == n and weights[0][5] == 1

        days = [dict(_decode(v)) for t in transactions for f, v in _decode(t) if f == 12]
        assert [d[1] for d in days] == [n, n + 1]
        today = days[0]
        assert (today[3], today[4]) == (500.0, 375.0)
        goals = dict(_decode(today[2]))
        assert goals[1] == 1905.0
        assert dict(_decode(goals[2])) == {1: 230.2, 2: 2405.0, 3: 2}

    def test_deleting_the_latest_weigh_in_restores_the_current_weight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self.NUM
        sent = self._install(
            monkeypatch,
            _database(
                [(n - 8, 230.2), (n, 150.0)],
                [(n - 1, 230.2, 2405.0, 2, 1905.0, 0.0, 0.0), (n, 150.0, 1968.9, 2, 1968.9, 0.0, 0.0)],
            ),
        )
        delete_weight(self.Http(), self.DAY)

        assert len(sent) == 3
        transactions = [t for body in sent for f, t in _decode(body) if f == 1]
        assert [f for f, _ in _decode(transactions[1])] == [1, 12, 16]
        assert [f for f, _ in _decode(transactions[2])] == [1, 11, 16]
        bag = dict(_decode(dict(_decode(transactions[2]))[11]))
        assert bag[1] == b"Goals"
        assert dict(_decode(bag[2])) == {1: b"currentWeight", 2: b"230.2"}

    def test_deleting_an_earlier_weigh_in_leaves_the_current_weight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self.NUM
        sent = self._install(
            monkeypatch,
            _database(
                [(n - 8, 230.2), (n, 150.0), (n + 3, 228.0)],
                [(n - 1, 230.2, 2405.0, 2, 1905.0, 0.0, 0.0)],
            ),
        )
        delete_weight(self.Http(), self.DAY)

        transactions = [t for body in sent for f, t in _decode(body) if f == 1]
        assert 11 not in [f for t in transactions for f, _ in _decode(t)]

    def test_a_day_without_a_weigh_in_is_refused_without_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._install(monkeypatch, _database([(self.NUM - 1, 200.0)], []))
        with pytest.raises(ValueError, match="No weigh-in"):
            delete_weight(self.Http(), self.DAY)
        assert sent == []

    def test_the_first_weigh_in_is_deleted_without_rewriting_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self.NUM
        sent = self._install(
            monkeypatch,
            _database([(n, 150.0)], [(n, 150.0, 1968.9, 2, 1968.9, 0.0, 0.0)]),
        )
        delete_weight(self.Http(), self.DAY)
        assert len(sent) == 1
        transactions = [t for f, t in _decode(sent[0]) if f == 1]
        fields = [f for t in transactions for f, _ in _decode(t)]
        assert 6 in fields and 12 not in fields and 11 not in fields

    def test_an_unacknowledged_delete_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _database([(self.NUM, 150.0)], []), ack=False)
        with pytest.raises(LoseItError, match="did not confirm"):
            delete_weight(self.Http(), self.DAY)


class TestGatewayPost:
    class Http:
        token = "TOKEN"

        class config:
            hours_from_gmt = -6
            user_id = "10313492"

    def _respond(self, monkeypatch: pytest.MonkeyPatch, status: int) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def fake_post(url: str, **kwargs: Any) -> httpx.Response:
            request = httpx.Request(
                "POST", url, headers=kwargs["headers"], content=kwargs["content"]
            )
            seen.append(request)
            return httpx.Response(status, content=b"body", request=request)

        monkeypatch.setattr(W.httpx, "post", fake_post)
        return seen

    def test_posts_with_the_account_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._respond(monkeypatch, 200)
        assert W._gateway_post(self.Http(), "/user/x", b"in") == b"body"
        assert seen[0].url == "https://gateway.loseit.com/user/x"
        assert seen[0].headers["authorization"] == "Bearer TOKEN"
        assert seen[0].headers["x-loseit-hoursfromgmt"] == "-6"
        assert seen[0].headers["x-loseit-device"].startswith("DROID-UID-")

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credentials_raise_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        self._respond(monkeypatch, status)
        with pytest.raises(LoseItAuthError):
            W._gateway_post(self.Http(), "/user/x", b"")

    def test_other_failures_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._respond(monkeypatch, 500)
        with pytest.raises(LoseItError, match="500"):
            W._gateway_post(self.Http(), "/user/x", b"")


class TestProtobufReader:
    def test_reads_every_wire_type(self) -> None:
        data = (
            W._varint_field(1, 300)
            + W._double_field(2, 1.5)
            + W._message_field(3, b"ab")
            + W._key(4, 5)
            + (7).to_bytes(4, "little")
        )
        assert _decode(data) == [(1, 300), (2, 1.5), (3, b"ab"), (4, 7)]
