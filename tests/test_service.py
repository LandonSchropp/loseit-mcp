"""Service layer: serialization, portions, dates, nutrients, and auth retry."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from lose_it import FoodLogEntry
from lose_it.core import daily as _daily
from lose_it.core._http import LoseItAuthError, LoseItError

from loseit_mcp.service import (
    _NUTRIENT_ORDINALS,
    _SENDABLE_ORDINALS,
    LoseItService,
    _entry_to_dict,
    _is_auth_failure,
    _logged_portions,
    _parse_date,
    _to_dict,
    _unescape,
    account_today,
)


class TestAuthFailureClassification:
    """Regression: Lose It reports auth failures as HTTP 200 GWT envelopes,
    which the SDK surfaces as a plain LoseItError. Matching only on
    LoseItAuthError meant transparent re-auth never fired."""

    def test_gwt_authentication_exception_is_an_auth_failure(self) -> None:
        exc = LoseItError(
            "GWT error: com.loseit.core.client.service."
            "UserAuthenticationFailedException/2362299878"
        )
        assert _is_auth_failure(exc)

    def test_transport_401_is_an_auth_failure(self) -> None:
        assert _is_auth_failure(LoseItAuthError("HTTP 401"))

    def test_unrelated_gwt_error_is_not(self) -> None:
        assert not _is_auth_failure(LoseItError("GWT error: SomeOtherProblem"))

    def test_unrelated_exception_is_not(self) -> None:
        assert not _is_auth_failure(ValueError("nope"))


class TestRetrying:
    def test_retries_once_after_reauthenticating(self, settings: Any) -> None:
        svc = LoseItService(settings)
        calls: list[int] = []
        svc._reauthenticate = lambda seen_generation=None: calls.append(1)  # type: ignore[method-assign]

        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise LoseItError("GWT error: UserAuthenticationFailedException/1")
            return "ok"

        assert svc._retrying(flaky) == "ok"
        assert calls == [1], "should re-authenticate exactly once"

    def test_does_not_retry_non_auth_errors(self, settings: Any) -> None:
        svc = LoseItService(settings)
        calls: list[int] = []
        svc._reauthenticate = lambda seen_generation=None: calls.append(1)  # type: ignore[method-assign]

        with pytest.raises(LoseItError):
            svc._retrying(lambda: (_ for _ in ()).throw(LoseItError("GWT error: Boom")))
        assert calls == [], "must not re-authenticate on unrelated failures"

    def test_reauth_failure_clears_stale_session(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: a failed re-login left the rejected session in place."""
        from loseit_mcp import service as S

        svc = LoseItService(settings)
        svc._session = object()  # type: ignore[assignment]
        monkeypatch.setattr(
            S, "resolve_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
        )
        with pytest.raises(RuntimeError):
            svc._reauthenticate()
        assert svc._session is None


class TestDates:
    """Regression: relative dates resolved against the host clock, so a server
    in another timezone logged food to the wrong day near midnight."""

    def test_today_uses_account_offset(self) -> None:
        # A far-eastern offset can legitimately be a day ahead of UTC.
        ahead = account_today(14)
        behind = account_today(-12)
        assert (ahead - behind).days in (0, 1)

    def test_yesterday_is_relative_to_account_today(self) -> None:
        assert _parse_date("yesterday", -7) == account_today(-7) - timedelta(days=1)

    def test_iso_dates_pass_through(self) -> None:
        assert _parse_date("2026-07-25", -7) == date(2026, 7, 25)

    @pytest.mark.parametrize("bad", ["25-07-2026", "tomorrow", "2026-13-01", "garbage"])
    def test_invalid_dates_raise(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _parse_date(bad, -7)

    def test_none_and_date_pass_through(self) -> None:
        assert _parse_date(None, -7) is None
        assert _parse_date(date(2026, 1, 1), -7) == date(2026, 1, 1)


class TestNutrients:
    """Regression: saturated_fat_g was reported as logged but silently dropped
    by the SDK's payload builder."""

    def test_saturated_fat_is_known_to_be_unsendable(self) -> None:
        assert _NUTRIENT_ORDINALS["saturated_fat_g"] not in _SENDABLE_ORDINALS

    def test_other_macros_are_sendable(self) -> None:
        for name in ("calories", "protein_g", "carb_g", "total_fat_g", "fiber_g", "sodium_mg"):
            assert _NUTRIENT_ORDINALS[name] in _SENDABLE_ORDINALS, name

    def test_dry_run_reports_dropped_nutrients(self, settings: Any) -> None:
        svc = LoseItService(settings)
        result = svc.log_custom_food(
            "Test", calories=500, fat_g=20, saturated_fat_g=9, protein_g=30, dry_run=True
        )
        assert result["ignored_nutrients"] == ["saturated_fat_g"]
        assert "saturated_fat_g" not in result["nutrients"]
        assert result["nutrients"]["protein_g"] == 30
        assert "warning" in result

    def test_no_warning_when_everything_is_sendable(self, settings: Any) -> None:
        svc = LoseItService(settings)
        result = svc.log_custom_food("Test", calories=100, protein_g=5, dry_run=True)
        assert "ignored_nutrients" not in result
        assert "warning" not in result

    def test_servings_scale_reported_values(self, settings: Any) -> None:
        svc = LoseItService(settings)
        result = svc.log_custom_food("Test", calories=100, protein_g=10, servings=2.5, dry_run=True)
        assert result["calories"] == 250
        assert result["nutrients"]["protein_g"] == 25


class TestPortionValidation:
    """Regression: a lone serving_unit was ignored, and combining servings with
    an amount silently discarded the servings value."""

    def test_amount_without_unit_raises(self, settings: Any) -> None:
        svc = LoseItService(settings)
        with pytest.raises(ValueError, match="together"):
            svc.log_food("a" * 32, serving_amount=120, dry_run=True)

    def test_unit_without_amount_raises(self, settings: Any) -> None:
        svc = LoseItService(settings)
        with pytest.raises(ValueError, match="together"):
            svc.log_food("a" * 32, serving_unit="g", dry_run=True)

    def test_servings_combined_with_amount_raises(self, settings: Any) -> None:
        svc = LoseItService(settings)
        with pytest.raises(ValueError, match="not both"):
            svc.log_food("a" * 32, servings=2, serving_amount=120, serving_unit="g", dry_run=True)


class TestSerialization:
    def test_unescapes_gwt_unicode_sequences(self) -> None:
        """Regression: food names arrived as 'Mike\\u0027s'."""
        assert _unescape("Jersey Mike\\u0027s") == "Jersey Mike's"

    def test_to_dict_unescapes_nested_strings(self) -> None:
        assert _to_dict({"a": ["Mike\\u0027s"]}) == {"a": ["Mike's"]}

    def test_to_dict_handles_primitives(self) -> None:
        assert _to_dict(None) is None
        assert _to_dict(3) == 3
        assert _to_dict(True) is True

    def test_an_entry_shows_the_amount_it_was_logged_in(self) -> None:
        entry = _entry_to_dict(
            {"servings": 0.333333, "food_measure_unit": "cup"}, portion=(1.0, "cup")
        )
        assert (entry["amount"], entry["unit"], entry["servings"]) == (1.0, "cup", 0.333)

    def test_an_entry_without_a_logged_amount_falls_back_to_servings(self) -> None:
        entry = _entry_to_dict({"servings": 2.0, "food_measure_unit": "serving"})
        assert (entry["amount"], entry["unit"]) == (2.0, "serving")

    def test_entry_projection_tolerates_missing_calories(self) -> None:
        entry = _entry_to_dict({"food_name": "X", "nutrients_by_label": {}})
        assert entry["calories"] is None
        assert entry["food_name"] == "X"


def _fle(entry_pk: list[int], quantity: float, ordinal: int, unit: str) -> dict[str, Any]:
    """A decoded FoodLogEntry, cut down to the parts that carry the portion."""
    return {
        "__type__": _daily._FLE_FQCN,
        "f2": {
            "f1": {
                "__type__": _daily._FOOD_SERVING_SIZE_FQCN,
                "f0": 1.0,
                "f2": {"ordinal": ordinal, "unit": unit},
                "f5": quantity,
            }
        },
        "f6": {"f0": entry_pk},
    }


class TestLoggedPortions:
    def test_it_reads_the_amount_each_entry_was_logged_in(self) -> None:
        decoded = {"f3": [_fle([1] * 16, 1.0, 3, "cup"), _fle([2] * 16, 185.0, 8, "grams")]}
        assert _logged_portions(decoded) == {
            "01" * 16: (1.0, "cup"),
            "02" * 16: (185.0, "grams"),
        }

    def test_it_labels_the_ounce_ordinal(self) -> None:
        assert _logged_portions(_fle([1] * 16, 4.0, 6, "unknown_ord_6")) == {"01" * 16: (4.0, "oz")}

    def test_it_skips_an_entry_with_no_serving_size(self) -> None:
        fle = _fle([1] * 16, 1.0, 3, "cup")
        fle["f2"] = None
        assert _logged_portions(fle) == {}

    def test_the_diary_shows_logged_amounts(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = FoodLogEntry(
            food_category="", food_name="Sushi", food_brand="", food_pk_response=[],
            entry_pk_response=[2] * 16, entry_day_key="", context_day_key="", day_num=0,
            hours_from_gmt=0, meal_ordinal=1, extra_ordinal=3, food_measure_ordinal=8,
            servings=1.0, food_identifier_code="", nutrients_ordered=[(0, 240.0)],
        )
        monkeypatch.setattr(_daily, "get_daily_details_raw", lambda http, day: "raw")
        monkeypatch.setattr(_daily, "parse_entries", lambda text, **kw: [entry])
        monkeypatch.setattr(
            "loseit_mcp.service.decode_response", lambda text: _fle([2] * 16, 185.0, 8, "grams")
        )
        svc = LoseItService(settings)
        config = type("Config", (), {"hours_from_gmt": 0, "user_name": ""})()
        svc._client = type("C", (), {"http": type("H", (), {"config": config})()})()

        [item] = svc.get_diary("2026-10-13")["entries"]
        assert (item["amount"], item["unit"], item["calories"]) == (185.0, "grams", 240.0)
