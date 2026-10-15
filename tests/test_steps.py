"""The day's step count, read through ``getCustomGoalValues``."""

from __future__ import annotations

from datetime import date

from loseit_mcp.steps import get_steps

# Captured from the web app for 2026-10-14 (day 9418), with the account swapped out.
_TYPES = (
    '"com.loseit.core.client.service.responses.LoseItRemoteServiceResponse/1071940969",'
    '"java.lang.Integer/3438268394","com.loseit.core.client.model.UserId/4281239478","Tester",'
    '"[Lcom.loseit.core.client.model.CustomGoalValue;/3340358626"'
)
_STEPS_RESPONSE = (
    "//OK[0,32,-71,-98,65,36,-13,61,-126,-87,71,-19,19,-63,100,-2,-96,16,8,7,"
    '"aE93U3g",8140.0,"aE93Un4",10,-1.0,11,0,-7,9418,"aE5bOfo",10,9,'
    "-110,-62,11,-1,-106,62,11,-82,-121,68,-106,-125,23,68,121,-122,16,8,7,6,1,5,"
    '"aE93eog",-7,4,10313492,3,40,2,1,[' + _TYPES + ","
    '"com.loseit.core.client.model.CustomGoalValue/1455343169",'
    '"com.loseit.core.client.model.SimplePrimaryKey/3621315060","[B/3308590456",'
    '"com.loseit.core.shared.model.DayDate/1611136587","java.util.Date/3385151746",'
    '"java.lang.Double/858496421"],0,7]'
)
_EMPTY_RESPONSE = '//OK[0,0,5,"aE941jA",-7,4,10313492,3,40,2,1,[' + _TYPES + "],0,7]"


class FakeConfig:
    base_url = "https://example.invalid/web/"
    policy_hash = "POLICY"
    user_id = "10313492"
    user_name = "Tester"
    hours_from_gmt = -7


class FakeHttp:
    config = FakeConfig()

    def __init__(self, response: str) -> None:
        self.response = response
        self.payloads: list[str] = []

    def post_rpc(self, payload: str) -> str:
        self.payloads.append(payload)
        return self.response


def test_reads_the_days_steps() -> None:
    http = FakeHttp(_STEPS_RESPONSE)

    assert get_steps(http, date(2026, 10, 14)) == 8140
    assert http.payloads == [
        (
            "7|0|11|https://example.invalid/web/|POLICY|"
            "com.loseit.core.client.service.LoseItRemoteService|getCustomGoalValues|"
            "com.loseit.core.client.service.ServiceRequestToken/1076571655|"
            "java.lang.String/2004016611|com.loseit.core.shared.model.DayDate/1611136587|"
            "com.loseit.core.client.model.UserId/4281239478|Tester|steps|"
            "java.util.Date/3385151746|"
            "1|2|3|4|3|5|6|7|5|0|8|10313492|9|-7|10|7|11|ZZZZZZZ|9418|-7|"
        )
    ]


def test_a_day_without_steps_is_none() -> None:
    assert get_steps(FakeHttp(_EMPTY_RESPONSE), date(2027, 6, 1)) is None
