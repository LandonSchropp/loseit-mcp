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
from loseit_mcp.weight import WeightHistoryError


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


def _entry(name: str, calories: float) -> FoodLogEntry:
    return FoodLogEntry(
        food_category="", food_name=name, food_brand="", food_pk_response=[],
        entry_pk_response=[2] * 16, entry_day_key="", context_day_key="", day_num=0,
        hours_from_gmt=0, meal_ordinal=1, extra_ordinal=3, food_measure_ordinal=8,
        servings=1.0, food_identifier_code="", nutrients_ordered=[(0, calories)],
    )


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
        entry = _entry("Sushi", 240.0)
        monkeypatch.setattr("loseit_mcp.service.get_daily_details_raw", lambda http, day: "raw")
        monkeypatch.setattr("loseit_mcp.service.parse_entries", lambda text, **kw: [entry])
        monkeypatch.setattr("loseit_mcp.service.get_steps", lambda http, day: None)
        monkeypatch.setattr(
            "loseit_mcp.service.decode_response", lambda text: _fle([2] * 16, 185.0, 8, "grams")
        )
        svc = LoseItService(settings)
        config = type("Config", (), {"hours_from_gmt": 0, "user_name": ""})()
        svc._client = type("C", (), {"http": type("H", (), {"config": config})()})()

        [item] = svc.get_diary("2026-10-13")["entries"]
        assert (item["amount"], item["unit"], item["calories"]) == (185.0, "grams", 240.0)


class TestDeleteEntry:
    def test_deletes_without_writing_to_disk(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from lose_it import LoseIt
        from lose_it.core import entries as _entries

        home = tmp_path / "home"
        home.mkdir(mode=0o500)
        monkeypatch.setenv("HOME", str(home))
        entry = _entry("Squash", 560.0)
        deleted: list[FoodLogEntry] = []
        monkeypatch.setattr(_entries, "delete", lambda http, e: deleted.append(e))
        client = object.__new__(LoseIt)
        client.config = type("Config", (), {"user_name": ""})()
        client.http = None
        client.diary = lambda day: [entry]  # type: ignore[method-assign]
        svc = LoseItService(settings)
        svc._client = client

        result = svc.delete_entry("02" * 16, when="2026-10-18")

        assert deleted == [entry]
        assert result["entry"]["food_name"] == "Squash"


class TestDeleteWeight:
    def test_deletes_the_days_weigh_in(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import loseit_mcp.service as S

        seen: list[date] = []

        def fake_delete(http: Any, when: date) -> float:
            seen.append(when)
            return 150.0

        monkeypatch.setattr(S, "_delete_weight", fake_delete)
        svc = LoseItService(settings)
        svc._client = type("Client", (), {"http": None})()

        assert svc.delete_weight("2026-10-14") == {
            "action": "delete_weight",
            "date": "2026-10-14",
            "weight": 150.0,
        }
        assert seen == [date(2026, 10, 14)]


class FakeBuilds:
    """Stands in for the deploy tracker: Lose It deploys when told to."""

    def __init__(self, settings: Any) -> None:
        self.ids = (settings.strong_name, settings.policy_hash)
        self.deploy_on_force: tuple[str, str] | None = None
        self.forced = 0

    def current(
        self, base_url: str, strong_name: str, policy_hash: str, force: bool = False
    ) -> tuple[str, str]:
        if force:
            self.forced += 1
            if self.deploy_on_force:
                self.ids, self.deploy_on_force = self.deploy_on_force, None
        return self.ids


class TestFollowingDeploys:
    NEW = ("201A8433DE84E0887AA4B65F2AD4F842", "F5AA51DFF7600BE94631854AFC5DEFE9")
    REJECTED = LoseItError(
        "GWT error: com.google.gwt.user.client.rpc.IncompatibleRemoteServiceException/3936916533"
    )

    @pytest.fixture
    def builds(self, settings: Any, monkeypatch: pytest.MonkeyPatch) -> FakeBuilds:
        from loseit_mcp import build

        fake = FakeBuilds(settings)
        monkeypatch.setattr(build, "tracker", fake)
        return fake

    @pytest.fixture
    def svc(self, settings: Any, session: Any) -> LoseItService:
        svc = LoseItService(settings)
        svc._session = session
        return svc

    def sent_ids(self, svc: LoseItService) -> tuple[str, str]:
        config = svc.client.http.config
        return config.strong_name, config.policy_hash

    def failing_once(self, svc: LoseItService, exc: Exception) -> Any:
        attempts: list[tuple[str, str]] = []

        def call() -> str:
            attempts.append(self.sent_ids(svc))
            if len(attempts) == 1:
                raise exc
            return "ok"

        call.attempts = attempts  # type: ignore[attr-defined]
        return call

    def test_a_rejected_call_is_retried_on_the_new_build(
        self, svc: LoseItService, builds: FakeBuilds
    ) -> None:
        builds.deploy_on_force = self.NEW
        call = self.failing_once(svc, self.REJECTED)

        assert svc._retrying(call) == "ok"
        assert call.attempts[-1] == self.NEW

    def test_a_rejected_read_is_retried_on_the_new_build(
        self, svc: LoseItService, builds: FakeBuilds
    ) -> None:
        builds.deploy_on_force = self.NEW
        call = self.failing_once(svc, self.REJECTED)

        assert svc._reading(call) == "ok"
        assert call.attempts[-1] == self.NEW

    @pytest.mark.parametrize(
        "exc",
        [
            KeyError("No schema for 'com.loseit.core.client.model.GoalsSummary/26794011'"),
            WeightHistoryError("Lose It's response for 2026-09-18 could not be decoded."),
        ],
    )
    def test_an_undecodable_reply_also_checks_for_a_deploy(
        self, svc: LoseItService, builds: FakeBuilds, exc: Exception
    ) -> None:
        builds.deploy_on_force = self.NEW

        assert svc._reading(self.failing_once(svc, exc)) == "ok"
        assert builds.forced == 1

    def test_new_type_layouts_alone_justify_a_retry(
        self, svc: LoseItService, builds: FakeBuilds, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lose_it.core import _decoder

        def deploy_layouts_only(*args: Any, force: bool = False) -> tuple[str, str]:
            if force:
                monkeypatch.setattr(_decoder._catalog(), "permutation", "NEWER")
            return builds.ids

        monkeypatch.setattr(builds, "current", deploy_layouts_only)
        call = self.failing_once(svc, KeyError("No schema for 'x'"))

        assert svc._retrying(call) == "ok"

    @pytest.mark.parametrize("run", ["_retrying", "_reading"])
    def test_without_a_deploy_the_failure_stands(
        self, svc: LoseItService, builds: FakeBuilds, run: str
    ) -> None:
        with pytest.raises(LoseItError):
            getattr(svc, run)(self.failing_once(svc, self.REJECTED))
        assert builds.forced == 1

    def test_a_deploy_another_request_already_followed_still_retries(
        self, svc: LoseItService, builds: FakeBuilds
    ) -> None:
        def fail_while_another_request_follows_the_deploy() -> str:
            builds.ids = self.NEW  # The other request's forced check got here first.
            raise self.REJECTED

        attempts = iter(
            [fail_while_another_request_follows_the_deploy, lambda: self.sent_ids(svc)]
        )

        assert svc._retrying(lambda: next(attempts)()) == self.NEW

    def test_a_deploy_noticed_between_calls_switches_the_client(
        self, svc: LoseItService, builds: FakeBuilds
    ) -> None:
        svc._retrying(lambda: None)
        old = svc.client
        builds.ids = self.NEW

        assert svc._retrying(lambda: self.sent_ids(svc)) == self.NEW
        assert svc.client is not old

    def test_unrelated_failures_do_not_check(
        self, svc: LoseItService, builds: FakeBuilds
    ) -> None:
        with pytest.raises(LoseItError):
            svc._retrying(self.failing_once(svc, LoseItError("GWT error: Boom")))
        assert builds.forced == 0
