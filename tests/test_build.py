"""Discovering Lose It's current GWT build from its public bundles."""

from __future__ import annotations

from typing import Any

import pytest
from lose_it.core import _decoder

from loseit_mcp import build as B

BASE = "https://cdn.invalid/web/"
PERM = "201A8433DE84E0887AA4B65F2AD4F842"
OTHER_PERM = "495281CED5C78EC4F017002F8D355172"
POLICY = "F5AA51DFF7600BE94631854AFC5DEFE9"

# Reader primitives, shaped like the compiled output (stack-trace bookkeeping
# included) but with names that match no previous build.
READERS = (
    "function rObj(a){var b,c;Q[c=++R]=rObj;b=a.b[--a.a];if(b<0){return a.e[-(b+1)]}"
    "c=rStr(a,b);R=c-1;return c}\\n"
    "function rStr(b,a){var c;Q[c=++R]=rStr;R=c-1;return a>0?b.d[a-1]:null}\\n"
    "function rBool(a){var b,c;Q[c=++R]=rBool;b=!!a.b[--a.a];R=c-1;return b}\\n"
    "function rDbl(a){var b,c;Q[c=++R]=rDbl;b=Number(a.b[--a.a]);R=c-1;return b}\\n"
    "function rLong(b){var a=b.b[--b.a],c,d;Q[d=++R]=rLong;c=b64(a);R=d-1;return c}\\n"
)

MAIN = (
    "web.onScriptDownloaded([\"var $strongName = '"
    + PERM
    + "';"
    + READERS
    + "function Proxy(){F.call(this,Vh(),'remote_logging','9AB6A9ABEA1CB0CAC3CEB3A9F185574E',S)}"
    + "function Main(){F.call(this,Vh(),null,'"
    + POLICY
    + "',T)}"
    + "var Wv='com.loseit.core.client.model.Widget/111',"
    + "Kv='com.loseit.core.client.model.Kind/222',"
    + "Bv='com.loseit.core.client.model.Base/333',"
    + "Ov='com.loseit.core.client.model.Orphan/444',"
    + "Lv='[Lcom.loseit.core.client.model.Widget;/555';"
    + "function iW(a){var b,c;Q[c=++R]=iW;b=new W;R=c-1;return b}"
    + "function dW(a,b){var c;Q[c=++R]=dW;dB(a,b);sW(b,rStr(a,a.b[--a.a]));"
    + "b.n=rBool(a);b.d=rDbl(a);b.l=rLong(a);b.o=cast(rObj(a),12);b.r=a.b[--a.a];R=c-1}"
    + "function dB(a,b){var c;Q[c=++R]=dB;b.x=rDbl(a);R=c-1}"
    + "function iK(a){var b,c,d;Q[d=++R]=iK;b=a.b[--a.a];c=(K(),[k0,k1]);R=d-1;return c[b]}"
    + "function dK(a,b){}"
    + "function iL(a){var b,c,d;Q[d=++R]=iL;b=a.b[--a.a];c=mk(X,b);R=d-1;return c}"
    + "function dL(a,b){}"
    + "var Sv='com.loseit.core.client.model.Sibling/777',"
    + "Nv='com.loseit.core.client.model.Untabled/888';"
    + "function dS(a,b){var c;Q[c=++R]=dS;dB(a,b);undefinedFn(a,b);dS(a,b);b.h=helper(a);R=c-1}"
    + "function helper(a){return a.size}"
    + "a[Sv]=[iS,dS];"
    + "a[Wv]=[iW,dW,sW];a[Kv]=[iK,dK];a[Bv]=[iB,dB];a[Ov]=[iO,dGone];a[Lv]=[iL,dL]"
    + '"])'
)

FRAGMENT = (
    "$wnd.web.runAsyncCallback1(\"var Fv='com.loseit.core.client.model.Late/666';"
    "function iF(a){var b,c;Q[c=++R]=iF;b=new Fz;R=c-1;return b}"
    "function dF(a,b){var c;Q[c=++R]=dF;b.s=rStr(a,a.b[--a.a]);R=c-1}"
    'a[Fv]=[iF,dF];\\n")'
)

NOCACHE = (
    "function web(){var Fb='" + OTHER_PERM + "',Gb='" + PERM + "',Hb='gecko1_8';"
    "Pb([Hb],Fb);Pb([Hb],Gb)}"
)


def fake_get(files: dict[str, str]) -> Any:
    calls: list[str] = []

    def get(url: str) -> str | None:
        calls.append(url)
        return files.get(url)

    get.calls = calls  # type: ignore[attr-defined]
    return get


def bundle_files() -> dict[str, str]:
    return {
        f"{BASE}web.nocache.js": NOCACHE,
        f"{BASE}{OTHER_PERM}.cache.js": MAIN,
        f"{BASE}deferredjs/{OTHER_PERM}/1.cache.js": FRAGMENT,
    }


@pytest.fixture(autouse=True)
def _restore_catalog() -> Any:
    saved = _decoder._CATALOG
    yield
    _decoder._CATALOG = saved


class TestExtractSchemas:
    def test_reads_every_field_kind_in_order_with_inherited_fields_first(self) -> None:
        schemas = B.extract_schemas(MAIN)

        assert schemas["com.loseit.core.client.model.Widget/111"] == {
            "fields": ["DOUBLE", "STRING", "BOOLEAN", "DOUBLE", "LONG", "OBJECT", "RAW"],
            "is_enum": False,
        }

    def test_shares_parent_fields_and_ignores_calls_that_read_nothing(self) -> None:
        schemas = B.extract_schemas(MAIN)

        assert schemas["com.loseit.core.client.model.Sibling/777"]["fields"] == ["DOUBLE"]
        assert "com.loseit.core.client.model.Untabled/888" not in schemas

    def test_marks_enums(self) -> None:
        schemas = B.extract_schemas(MAIN)

        assert schemas["com.loseit.core.client.model.Kind/222"] == {
            "fields": [],
            "is_enum": True,
        }

    def test_arrays_are_not_enums(self) -> None:
        schemas = B.extract_schemas(MAIN)

        assert schemas["[Lcom.loseit.core.client.model.Widget;/555"]["is_enum"] is False

    def test_skips_types_whose_functions_are_missing(self) -> None:
        schemas = B.extract_schemas(MAIN)

        assert "com.loseit.core.client.model.Orphan/444" not in schemas
        # The table names an instantiate function that was never defined.
        assert schemas["com.loseit.core.client.model.Base/333"] == {
            "fields": ["DOUBLE"],
            "is_enum": False,
        }

    def test_fails_loudly_when_no_reader_is_recognized(self) -> None:
        with pytest.raises(B.BuildError, match="reader"):
            B.extract_schemas("var Wv='com.loseit.x.Y/1';a[Wv]=[i,d]")


class TestDiscover:
    def test_reads_identifiers_and_schemas_from_the_bundles(self) -> None:
        build = B.discover(BASE, fake_get(bundle_files()))

        assert build.strong_name == OTHER_PERM
        assert build.policy_hash == POLICY
        assert build.schemas["com.loseit.core.client.model.Late/666"]["fields"] == ["STRING"]

    def test_stops_at_the_first_missing_fragment(self) -> None:
        get = fake_get(bundle_files())

        B.discover(BASE, get)

        assert get.calls[-1] == f"{BASE}deferredjs/{OTHER_PERM}/2.cache.js"

    @pytest.mark.parametrize(
        ("path", "content", "message"),
        [
            ("web.nocache.js", None, "bootstrap"),
            ("web.nocache.js", "no hashes here", "permutation"),
            (f"{OTHER_PERM}.cache.js", None, "bundle"),
            (f"{OTHER_PERM}.cache.js", MAIN.replace(POLICY, "nope"), "policy"),
        ],
    )
    def test_explains_what_it_could_not_find(
        self, path: str, content: str | None, message: str
    ) -> None:
        files: dict[str, Any] = bundle_files()
        files[f"{BASE}{path}"] = content

        with pytest.raises(B.BuildError, match=message):
            B.discover(BASE, fake_get(files))

    def test_permutations_only_fetches_the_bootstrap(self) -> None:
        get = fake_get(bundle_files())

        assert B.permutations(BASE, get) == [OTHER_PERM, PERM]
        assert get.calls == [f"{BASE}web.nocache.js"]


class TestInstall:
    def test_the_decoder_reads_types_from_the_new_build(self) -> None:
        before = set(_decoder._catalog().schemas)
        build = B.discover(BASE, fake_get(bundle_files()))

        B.install(build)

        catalog = _decoder._catalog()
        assert catalog.permutation == OTHER_PERM
        assert catalog.schemas["com.loseit.core.client.model.Late/666"].fields == ["STRING"]
        assert catalog.schemas["com.loseit.core.client.model.Kind/222"].is_enum
        # Types the bundles don't describe keep decoding.
        assert before <= set(catalog.schemas)


class TestHttpGet:
    def test_returns_text_on_success_and_none_on_404(self) -> None:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("missing.js"):
                return httpx.Response(404)
            return httpx.Response(200, text="body")

        get = B.http_get(httpx.Client(transport=httpx.MockTransport(handler)))

        assert get("https://cdn.invalid/web/here.js") == "body"
        assert get("https://cdn.invalid/web/missing.js") is None

    def test_other_errors_raise(self) -> None:
        import httpx

        get = B.http_get(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))

        with pytest.raises(httpx.HTTPStatusError):
            get("https://cdn.invalid/web/x.js")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


STALE = ("351AE5DC0CA36AD3BA9C7CBA7B0E07B8", "8F87EC8969F17AE77B6283D3A83F6D4C")


def tracker(files: dict[str, Any] | None = None) -> tuple[B.Tracker, Any, FakeClock]:
    get = fake_get(bundle_files() if files is None else files)
    clock = FakeClock()
    return B.Tracker(get, clock=clock), get, clock


class TestTracker:
    def test_switches_to_the_live_build_when_its_own_is_gone(self) -> None:
        t, _, _ = tracker()

        assert t.current(BASE, *STALE) == (OTHER_PERM, POLICY)
        assert _decoder._catalog().permutation == OTHER_PERM

    def test_keeps_the_discovered_build_afterwards(self) -> None:
        t, get, _ = tracker()
        t.current(BASE, *STALE)
        get.calls.clear()

        assert t.current(BASE, *STALE) == (OTHER_PERM, POLICY)
        assert get.calls == []

    def test_rechecks_only_the_bootstrap_once_the_interval_passes(self) -> None:
        t, get, clock = tracker()
        t.current(BASE, *STALE)
        get.calls.clear()
        clock.now += B.CHECK_INTERVAL

        assert t.current(BASE, *STALE) == (OTHER_PERM, POLICY)
        assert get.calls == [f"{BASE}web.nocache.js"]

    def test_a_forced_check_waits_a_shorter_cooldown(self) -> None:
        t, get, clock = tracker()
        t.current(BASE, *STALE)
        get.calls.clear()

        t.current(BASE, *STALE, force=True)
        assert get.calls == []

        clock.now += B.FORCED_CHECK_INTERVAL
        t.current(BASE, *STALE, force=True)
        assert get.calls == [f"{BASE}web.nocache.js"]

    def test_rediscovers_when_only_the_decoder_is_out_of_date(self) -> None:
        t, get, _ = tracker()

        t.current(BASE, OTHER_PERM, POLICY)

        assert f"{BASE}{OTHER_PERM}.cache.js" in get.calls
        assert _decoder._catalog().permutation == OTHER_PERM

    def test_leaves_a_current_build_alone(self) -> None:
        t, get, _ = tracker()
        B.install(B.Build(OTHER_PERM, POLICY, {}))
        get.calls.clear()

        assert t.current(BASE, PERM, "CONFIGURED") == (PERM, "CONFIGURED")
        assert get.calls == [f"{BASE}web.nocache.js"]

    def test_a_failed_check_keeps_the_identifiers_it_had(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        t, _, _ = tracker({})

        assert t.current(BASE, *STALE) == STALE
        assert "Could not check" in caplog.text
