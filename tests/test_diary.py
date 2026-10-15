"""The day's calorie budget, exercise and net calories alongside the diary."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from lose_it.core._http import LoseItAuthError

import loseit_mcp.service as service_module
from loseit_mcp.cli import _print_diary
from loseit_mcp.service import LoseItService

_MODEL = "com.loseit.core.client.model."


def _exercise(calories: float, category: str, name: str | None) -> dict[str, Any]:
    return {
        "__type__": _MODEL + "ExerciseLogEntry/3129076305",
        "f1": calories,
        "f4": {"__type__": _MODEL + "Exercise/768553247", "f4": category, "f5": name},
    }


def _decoded(budget: float, exercises: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "f3": {
            "__type__": _MODEL + "DailyDetails/1565598417",
            "f3": {
                "__type__": _MODEL + "DailyLogEntry/612926967",
                "f6": {"__type__": _MODEL + "DailyLogGoalsState/1324910267", "f0": budget},
            },
            "f4": {
                "__type__": "[L" + _MODEL + "ExerciseLogEntry;/1448715428",
                "items": exercises,
            },
            "f6": {"__type__": _MODEL + "GoalsSummary/907689655", "f5": 1850.933},
        }
    }


class _Entry:
    def __init__(self, calories: float) -> None:
        self.calories = calories

    def to_dict(self) -> dict[str, Any]:
        return {"meal": "lunch", "nutrients_by_label": {"calories": self.calories}}


def _service(
    monkeypatch: pytest.MonkeyPatch, decoded: Any, food: list[float], steps: int | None = 8140
) -> LoseItService:
    svc = LoseItService.__new__(LoseItService)
    svc._settings = type("S", (), {"hours_from_gmt": 0})()
    svc._lock = threading.RLock()
    config = type("Config", (), {"hours_from_gmt": 0, "user_name": "Tester"})()
    svc._client = type("C", (), {"http": type("H", (), {"config": config})()})()
    svc._inflight = 0
    svc._retired = []
    svc._generation = 0
    monkeypatch.setattr(service_module, "get_daily_details_raw", lambda http, day: "RAW")
    monkeypatch.setattr(service_module, "decode_response", lambda text: decoded)
    monkeypatch.setattr(service_module, "get_steps", lambda http, day: steps)
    monkeypatch.setattr(
        service_module, "parse_entries", lambda text, **kwargs: [_Entry(c) for c in food]
    )
    return svc


class TestGetDiary:
    def test_under_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        decoded = _decoded(
            1905.0,
            [
                _exercise(106.0, "Steps Calorie Bonus", None),
                _exercise(269.291, "HealthKit Workout", "Strength Training"),
            ],
        )
        day = _service(monkeypatch, decoded, [1000.0, 142.1]).get_diary("2026-10-14")

        assert day["calorie_budget"] == 1905
        assert day["exercise"] == [
            {"name": "Steps Calorie Bonus", "calories": 106.0},
            {"name": "Strength Training", "calories": 269.3},
        ]
        assert day["exercise_calories"] == 375.3
        assert day["net_calories"] == 766.8
        assert day["calories_under_budget"] == 1138.2
        assert day["steps"] == 8140

    def test_over_budget_is_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = _service(monkeypatch, _decoded(1905.0, []), [2000.0]).get_diary("2026-10-14")

        assert day["exercise"] == []
        assert day["net_calories"] == 2000
        assert day["calories_under_budget"] == -95

    def test_auth_retry_uses_the_new_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = _service(monkeypatch, _decoded(1905.0, []), [])
        expired = svc._client.http
        renewed = type("C", (), {"http": type("H", (), {"config": expired.config})()})()
        calls = []

        def fetch(http: Any, day: Any) -> str:
            calls.append(http)
            if http is expired:
                raise LoseItAuthError("expired")
            return "RAW"

        monkeypatch.setattr(service_module, "get_daily_details_raw", fetch)
        monkeypatch.setattr(svc, "_reauthenticate", lambda **kw: setattr(svc, "_client", renewed))

        svc.get_diary("2026-10-14")
        assert calls == [expired, renewed.http]

    def test_missing_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response the decoder can't read must not invent a budget."""
        day = _service(monkeypatch, None, [500.0]).get_diary("2026-10-14")

        assert day["calorie_budget"] is None
        assert day["calories_under_budget"] is None
        assert day["exercise"] == []
        assert day["net_calories"] == 500

    def test_no_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        day = _service(monkeypatch, _decoded(1905.0, []), [], steps=None).get_diary("2026-10-14")

        assert day["steps"] is None


def _day(**overrides: Any) -> dict[str, Any]:
    return {
        "date": "2026-10-13",
        "entry_count": 0,
        "total_calories": 1142.1,
        "entries": [],
        "calorie_budget": 1905,
        "exercise": [{"name": "Steps Calorie Bonus", "calories": 186.0}],
        "exercise_calories": 186.0,
        "net_calories": 956.1,
        "calories_under_budget": 948.9,
        "steps": 10104,
        **overrides,
    }


class TestPrintDiary:
    def test_under(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_diary(_day())
        out = capsys.readouterr().out
        assert "Budget 1905 · Food 1142 · Exercise 186 · Net 956 · Under 949" in out
        assert "EXERCISE (186 cal)" in out
        assert "186.0 cal  Steps Calorie Bonus" in out
        assert "Steps 10104" in out

    def test_over(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_diary(_day(exercise=[], calories_under_budget=-95.0))
        out = capsys.readouterr().out
        assert "Over 95" in out
        assert "EXERCISE" not in out

    def test_no_budget(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_diary(_day(calorie_budget=None, calories_under_budget=None))
        out = capsys.readouterr().out
        assert "Food 1142 · Exercise 186 · Net 956" in out
        assert "Budget" not in out and "Under" not in out

    def test_no_steps(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_diary(_day(steps=None, exercise=[]))
        assert "Steps" not in capsys.readouterr().out
