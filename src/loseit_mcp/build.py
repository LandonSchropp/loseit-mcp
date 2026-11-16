"""Discover Lose It's current GWT web build from its public bundles."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from lose_it.core import _decoder

Getter = Callable[[str], str | None]

logger = logging.getLogger(__name__)

# How often to ask whether Lose It has deployed, and how soon a failure that
# looks like a deploy may ask again.
CHECK_INTERVAL = 600.0
FORCED_CHECK_INTERVAL = 60.0

_HASH = r"[0-9A-F]{32}"
_IDENT = r"[A-Za-z_$][\w$]*"

_PERMUTATION_RE = re.compile(rf"'({_HASH})'")
# The main service proxy has no relative path: ``F.call(this,base(),null,'<policy>',ser)``.
_POLICY_RE = re.compile(rf"\.call\(this,[^,()]*\(\),null,'({_HASH})'")
_FUNCTION_RE = re.compile(rf"function ({_IDENT})\(([^)]*)\)\s*\{{")
_CONSTANT_RE = re.compile(
    rf"(?<![\w$])({_IDENT})\s*=\s*'((?:com\.loseit|\[Lcom\.loseit)[^']+/\d+)'"
)
_TABLE_RE = re.compile(rf"(?<![\w$])a\[({_IDENT})\]\s*=\s*\[({_IDENT})\s*,\s*({_IDENT})")
_SUPER_CALL_RE = re.compile(rf"^({_IDENT})\(a,b\)$")
_READ_RE = re.compile(rf"({_IDENT})\(a(?:,a\.b\[--a\.a\])?\)|a\.b\[--a\.a\]")
_ENUM_RETURN_RE = re.compile(r"return [a-z]\[[a-z]\]")


class BuildError(RuntimeError):
    """The bundles didn't contain what a build needs."""


@dataclass(frozen=True)
class Build:
    strong_name: str
    policy_hash: str
    schemas: dict[str, dict[str, Any]]


def http_get(client: httpx.Client) -> Getter:
    """A getter that returns a file's text, or ``None`` when it doesn't exist."""

    def get(url: str) -> str | None:
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    return get


def permutations(base_url: str, get: Getter) -> list[str]:
    """The permutations of the build Lose It is serving now."""
    bootstrap = get(f"{base_url}web.nocache.js")
    if bootstrap is None:
        raise BuildError("Lose It's web bootstrap script (web.nocache.js) is missing.")
    found = list(dict.fromkeys(_PERMUTATION_RE.findall(bootstrap)))
    if not found:
        raise BuildError("No permutation strong names found in web.nocache.js.")
    return found


def discover(base_url: str, get: Getter) -> Build:
    """Read the current build's identifiers and type layouts."""
    strong_name = permutations(base_url, get)[0]

    main = get(f"{base_url}{strong_name}.cache.js")
    if main is None:
        raise BuildError(f"The bundle for permutation {strong_name} is missing.")
    policy = _POLICY_RE.search(main)
    if policy is None:
        raise BuildError(f"No service policy hash found in the {strong_name} bundle.")

    parts = [main]
    n = 1
    while (fragment := get(f"{base_url}deferredjs/{strong_name}/{n}.cache.js")) is not None:
        parts.append(fragment)
        n += 1

    return Build(
        strong_name=strong_name,
        policy_hash=policy.group(1),
        schemas=extract_schemas("\n".join(parts)),
    )


def install(build: Build) -> None:
    """Make the decoder read types the way this build serializes them."""
    current = _decoder._catalog()
    catalog = _decoder._Catalog(permutation=build.strong_name, schemas=dict(current.schemas))
    for fqcn, info in build.schemas.items():
        catalog.schemas[fqcn] = _decoder._Schema(
            fqcn=fqcn, fields=info["fields"], is_enum=info["is_enum"]
        )
    _decoder._CATALOG = catalog


class Tracker:
    """Follows Lose It's deploys so RPCs always name the build being served."""

    def __init__(self, get: Getter, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._get = get
        self._clock = clock
        self._lock = threading.Lock()
        self._build: Build | None = None
        self._checked_at: float | None = None

    def current(
        self, base_url: str, strong_name: str, policy_hash: str, *, force: bool = False
    ) -> tuple[str, str]:
        """The ``(strong_name, policy_hash)`` to send, refreshed when stale.

        ``force`` is for a failure that looks like a deploy, and shortens the
        wait since the last check. A failed check keeps what it had.
        """
        # ponytail: one lock across the fetches, so a slow CDN mid-discovery
        # stalls other requests; check outside the lock if that ever bites.
        with self._lock:
            ids = (
                (self._build.strong_name, self._build.policy_hash)
                if self._build
                else (strong_name, policy_hash)
            )
            now = self._clock()
            wait = FORCED_CHECK_INTERVAL if force else CHECK_INTERVAL
            if self._checked_at is not None and now - self._checked_at < wait:
                return ids
            self._checked_at = now
            try:
                live = permutations(base_url, self._get)
                if ids[0] in live and _decoder._catalog().permutation in live:
                    return ids
                build = discover(base_url, self._get)
            except (BuildError, httpx.HTTPError) as exc:
                logger.warning("Could not check for a new Lose It build: %s", exc)
                return ids
            install(build)
            self._build = build
            logger.info("switched to Lose It build %s", build.strong_name)
            return build.strong_name, build.policy_hash


# ponytail: in memory only (hosted runs read-only), so each process start
# rediscovers once; persist it if startup cost ever matters.
tracker = Tracker(http_get(httpx.Client(timeout=30.0)))


def extract_schemas(js: str) -> dict[str, dict[str, Any]]:
    """Map each Lose It type to the field kinds its deserializer reads, in order."""
    return _Extractor(js).run()


class _Extractor:
    def __init__(self, js: str) -> None:
        self.js = js
        self.functions = {m.group(1): m for m in _FUNCTION_RE.finditer(js)}
        self.reader_kinds: dict[str, str | None] = {}
        self.field_cache: dict[str, list[str]] = {}

    def run(self) -> dict[str, dict[str, Any]]:
        table = {m.group(1): (m.group(2), m.group(3)) for m in _TABLE_RE.finditer(self.js)}
        schemas: dict[str, dict[str, Any]] = {}
        for var, fqcn in _CONSTANT_RE.findall(self.js):
            if var not in table:
                continue
            instantiate, deserialize = table[var]
            if deserialize not in self.functions:
                continue
            inst_body = self._body(instantiate) or ""
            schemas[fqcn] = {
                "fields": self._fields(deserialize, set()),
                "is_enum": "[--a.a]" in inst_body and bool(_ENUM_RETURN_RE.search(inst_body)),
            }
        if not any(info["fields"] for info in schemas.values()):
            raise BuildError("No GWT reader functions were recognized in the bundles.")
        return schemas

    def _body(self, name: str) -> str | None:
        match = self.functions.get(name)
        if match is None:
            return None
        depth, i = 1, match.end()
        while depth and i < len(self.js):
            depth += {"{": 1, "}": -1}.get(self.js[i], 0)
            i += 1
        return self.js[match.end() : i - 1]

    def _fields(self, name: str, seen: set[str]) -> list[str]:
        if name in self.field_cache:
            return self.field_cache[name]
        body = self._body(name)
        if body is None or name in seen:
            return []
        seen = seen | {name}
        fields: list[str] = []
        for statement in body.split(";"):
            statement = statement.strip()
            if parent := _SUPER_CALL_RE.match(statement):
                # GWT writes superclass fields first, via an inlined call.
                fields.extend(self._fields(parent.group(1), seen))
                continue
            for read in _READ_RE.finditer(statement):
                kind = "RAW" if read.group(1) is None else self._reader_kind(read.group(1))
                if kind:
                    fields.append(kind)
        self.field_cache[name] = fields
        return fields

    def _reader_kind(self, name: str) -> str | None:
        if name not in self.reader_kinds:
            self.reader_kinds[name] = _classify_reader(self._body(name) or "")
        return self.reader_kinds[name]


def _classify_reader(body: str) -> str | None:
    if "b.d[a-1]" in body:
        return "STRING"
    if "<0)" in body:
        return "OBJECT"
    if "!!a.b[--a.a]" in body:
        return "BOOLEAN"
    if "Number(a.b[--a.a])" in body:
        return "DOUBLE"
    if "[--b.a]" in body:
        return "LONG"
    return None
