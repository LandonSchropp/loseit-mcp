"""A day's step count via the ``getCustomGoalValues`` GWT-RPC method."""

from __future__ import annotations

from datetime import date

from lose_it.core._dates import day_number_for
from lose_it.core._gwt import build_envelope
from lose_it.core._http import HttpClient
from lose_it.core.daily import _walk_dicts, decode_response

from .weight import _DATE, _DAY_DATE, _FALLBACK_DAY_KEY, _REQUEST_TOKEN, _SERVICE, _USER_ID

_CUSTOM_GOAL_VALUE = "com.loseit.core.client.model.CustomGoalValue/"


def get_steps(http: HttpClient, when: date) -> int | None:
    """The steps recorded for ``when``, or ``None`` when there are none."""
    config = http.config
    strings = [
        config.base_url,
        config.policy_hash,
        _SERVICE,
        "getCustomGoalValues",
        _REQUEST_TOKEN,
        "java.lang.String/2004016611",
        _DAY_DATE,
        _USER_ID,
        config.user_name,
        "steps",  # 10: the goal's tag
        _DATE,
    ]
    tz = str(config.hours_from_gmt)
    data = [
        *["1", "2", "3", "4", "3", "5", "6", "7"],
        *["5", "0", "8", str(config.user_id), "9", tz, "10"],
        *["7", "11", _FALLBACK_DAY_KEY, str(day_number_for(when)), tz],
    ]

    response = decode_response(http.post_rpc(build_envelope(strings, data)))
    for node in _walk_dicts(response):
        if node.get("__type__", "").startswith(_CUSTOM_GOAL_VALUE):
            return round(node["f5"])
    return None
