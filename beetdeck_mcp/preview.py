"""Preview tokens — the record that binds a `preview_*` tool to `apply_change`.

Every mutating capability is split in two: a preview tool shows the diff and
mints a token, and `apply_change(preview_token)` commits it. This module owns the
token: what it carries, how long it lives, and what makes it refuse to apply.

**The freshness guarantee is not uniform, and the difference is named here rather
than implied.** Three strategies, one per group of kinds (`FRESHNESS_BY_KIND`):

* `BACKEND_PRECONDITION` (`identify`, `items_identify`) — the token carries the
  `match_generation` read at preview time and passes it to the confirm endpoint,
  which compares it inside `state.identify_lock`. A re-identify or a competing
  confirm is *rejected*, not merely noticed.
* `REREAD_OLD_VALUES` (`tags`, `genres`) — the token records the values it
  previewed; `apply_change` re-reads them immediately before writing and refuses
  on drift. TOCTOU-racy by construction: a change landing between the re-read and
  the write is not caught.
* `TTL_ONLY` (`cover`, `lyrics`) — the backend stash has no identity and no read
  endpoint, so a competing fetch cannot be detected from outside at all. These
  tokens get a short TTL and say so.

Refusals are deliberately *distinct*: expiry, value drift and a backend
precondition failure each produce their own reason and their own actionable
message, because a generic "apply failed" is what makes an agent retry blindly.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from beetdeck_mcp.client import ApplyResult

Freshness = Literal["backend_precondition", "reread_old_values", "ttl_only"]

BACKEND_PRECONDITION: Freshness = "backend_precondition"
REREAD_OLD_VALUES: Freshness = "reread_old_values"
TTL_ONLY: Freshness = "ttl_only"

#: Which strategy defends which kind. The keys are exactly `client.WRITE_KINDS`
#: — a write the MCP server can commit but cannot describe the guarantee for
#: would be the one thing this design must not ship.
FRESHNESS_BY_KIND: Mapping[str, Freshness] = {
    "identify": BACKEND_PRECONDITION,
    "items_identify": BACKEND_PRECONDITION,
    "tags": REREAD_OLD_VALUES,
    "genres": REREAD_OLD_VALUES,
    "cover": TTL_ONLY,
    "lyrics": TTL_ONLY,
}

#: Agent-facing wording, returned with every minted token. An agent that cannot
#: see how strong the promise is will treat all six kinds as equally safe.
GUARANTEE: Mapping[Freshness, str] = {
    BACKEND_PRECONDITION: (
        "Enforced by the backend: this token carries the identification generation read "
        "at preview time, and the confirm is rejected outright if identification was "
        "re-run or already applied in the meantime."
    ),
    REREAD_OLD_VALUES: (
        "Best effort: the previewed values are re-read immediately before writing and the "
        "apply is refused if they changed. A change landing between that re-read and the "
        "write itself is not caught."
    ),
    TTL_ONLY: (
        "Time-limited only: the backend keeps the fetched result in an anonymous stash "
        "that cannot be read back, so a competing fetch in the web UI overwrites it and "
        "wins silently. Apply promptly."
    ),
}

#: 15 minutes is long enough for an agent to think between preview and apply.
DEFAULT_TTL = 15 * 60.0
#: `cover`/`lyrics` get 5: nothing but the clock protects them.
SHORT_TTL = 5 * 60.0

#: How many burned tokens to remember, so a second apply can say *why* the token
#: is gone instead of "unknown". Purely diagnostic — bounded on purpose.
BURNED_HISTORY = 256

# --- refusal reasons --------------------------------------------------------

UNKNOWN_TOKEN = "unknown_token"
ALREADY_APPLIED = "already_applied"
#: The token was spent on an apply that came back `failed`. Distinct from
#: `ALREADY_APPLIED` because that wording asserts a write that did not happen —
#: a refused drift check or an unreachable backend would otherwise tell the
#: agent its change had landed.
APPLY_FAILED = "apply_failed"
EXPIRED = "expired"
DRIFT = "drift"
STALE_GENERATION = "stale_generation"


class PreviewError(RuntimeError):
    """An apply was refused. `reason` is one of the constants above."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason

    @property
    def stale(self) -> bool:
        """True when the library moved under the preview, as opposed to the
        token merely being gone or too old."""
        return self.reason in (DRIFT, STALE_GENERATION)

    def as_result(self) -> ApplyResult:
        """The refusal in the shape `apply_change` returns."""
        return ApplyResult("failed", (str(self),), stale=self.stale)


def freshness_for(kind: str) -> Freshness:
    """The strategy defending `kind`, or `ValueError` for an unknown kind."""
    try:
        return FRESHNESS_BY_KIND[kind]
    except KeyError:
        raise ValueError(
            f"unknown preview kind {kind!r}; known kinds: {tuple(FRESHNESS_BY_KIND)}"
        ) from None


def ttl_for(kind: str) -> float:
    """How long a token of `kind` stays applicable."""
    return SHORT_TTL if freshness_for(kind) == TTL_ONLY else DEFAULT_TTL


@dataclass(frozen=True)
class Preview:
    """One minted token and everything `apply_change` needs to commit it."""

    token: str
    kind: str
    #: Human-readable subject, e.g. `"album 12"` — it appears in every refusal.
    target: str
    method: str
    endpoint: str
    #: The request body to send on apply, minus the precondition field.
    params: dict[str, Any] = field(default_factory=dict)
    expected_generation: int | None = None
    old_values: dict[str, Any] | None = None
    #: Where `apply_change` re-reads `old_values` from, immediately before the
    #: write. Carried on the record rather than rebuilt at apply time: the read
    #: path is not derivable from the write path (a tag edit is written per item
    #: but only readable under its album), and a handler that guessed it wrong
    #: would compare against the wrong row and pass.
    reread_endpoint: str | None = None
    #: Passed to `client.write` for the lyrics short-write row (see client.py).
    requested_item_ids: tuple[int, ...] | None = None
    #: The diff the agent was shown; carried so an apply can echo what it did.
    summary: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0

    @property
    def freshness(self) -> Freshness:
        return FRESHNESS_BY_KIND[self.kind]

    @property
    def guarantee(self) -> str:
        return GUARANTEE[self.freshness]

    @property
    def ttl(self) -> float:
        return self.expires_at - self.created_at

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def request_json(self) -> dict[str, Any] | None:
        """The body to send on apply — `params` plus the backend precondition.

        `None` when there is nothing to send (a bodyless confirm, e.g. cover).
        """
        body = dict(self.params)
        if self.freshness == BACKEND_PRECONDITION:
            body["expected_generation"] = self.expected_generation
        return body or None


def _comparable(value: Any) -> Any:
    """Normalize a field value for drift comparison.

    `None` and `""` both mean "unset" on the tag endpoints — an empty value
    clears the field — so they must not read as a change. Lists are compared
    element-wise and order-significantly (genres are ordered).
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return tuple(_comparable(entry) for entry in value)
    # Rendered, not raw: the tag endpoints answer with beets' typed values
    # (`track` is an int, `year` an int, `bpm` a float) while an agent expresses
    # every new value as a string, so a raw comparison calls `{"track": "5"}` on
    # a track already at 5 a change. Rendering agrees with the backend's own
    # coercion (`item_fields.coerce_value`) for every scalar type — except a
    # bool spelled `"true"` against beets' stored `1`, which this side has no
    # field schema to resolve and which costs only a redundant write.
    return str(value)


def values_equal(left: Any, right: Any) -> bool:
    """Whether two field values mean the same thing.

    The one comparison rule shared by the preview producers (which decide what
    counts as a change) and the drift check below (which decides what counts as
    someone else's change) — two rules would eventually disagree about `""`.
    """
    return _comparable(left) == _comparable(right)


def check_old_values(preview: Preview, current: Mapping[str, Any]) -> None:
    """Re-read strategy: refuse the apply if anything moved since the preview.

    `current` is a fresh read of the same fields, taken immediately before the
    write. Raises `PreviewError(DRIFT)` naming every field that changed.
    """
    if preview.freshness != REREAD_OLD_VALUES:
        raise ValueError(
            f"{preview.kind!r} previews are defended by {preview.freshness!r}, "
            "not by re-reading old values"
        )
    assert preview.old_values is not None  # guaranteed by mint()

    drifted = [
        f"{name}: previewed {old!r}, now {current.get(name)!r}"
        for name, old in preview.old_values.items()
        if not values_equal(current.get(name), old)
    ]
    if drifted:
        raise PreviewError(
            DRIFT,
            f"the values changed since the preview of {preview.target} "
            f"({'; '.join(drifted)}) — nothing was written; re-run the preview to see the "
            "current state and decide again",
        )


def precondition_failed(preview: Preview, result: ApplyResult | None = None) -> PreviewError:
    """Backend-precondition strategy: turn a rejected confirm into a refusal.

    Called when the confirm came back `409` (`ApplyResult.stale`), which the
    backend answers when the identification generation no longer matches the one
    this token carries.
    """
    if preview.freshness != BACKEND_PRECONDITION:
        raise ValueError(
            f"{preview.kind!r} previews carry no backend precondition "
            f"(they are defended by {preview.freshness!r})"
        )
    detail = f" (backend said: {'; '.join(result.detail)})" if result and result.detail else ""
    return PreviewError(
        STALE_GENERATION,
        f"identification for {preview.target} was re-run or already applied since this "
        f"preview{detail} — nothing was written; identify again and preview the new "
        "candidates",
    )


@dataclass
class PreviewStore:
    """In-process token table: mint, look up, burn on use, expire.

    Single-use and thread-safe — the MCP SDK dispatches sync tool callables on a
    worker thread pool, so two applies of the same token can genuinely race.
    `clock` is injectable so tests need not sleep through a 15-minute TTL.
    """

    clock: Callable[[], float] = time.monotonic
    _records: dict[str, Preview] = field(default_factory=dict, init=False, repr=False)
    _burned: "OrderedDict[str, str]" = field(default_factory=OrderedDict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def mint(
        self,
        kind: str,
        *,
        target: str,
        method: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        expected_generation: int | None = None,
        old_values: Mapping[str, Any] | None = None,
        reread_endpoint: str | None = None,
        requested_item_ids: Sequence[int] | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> Preview:
        """Record a previewed change and return its token.

        The arguments a kind *must* carry are enforced here, not left to each
        preview tool: an identification token without a generation, or a tags
        token without old values, would silently downgrade to a TTL-only
        promise while still being described as a strong one.
        """
        strategy = freshness_for(kind)
        if strategy == BACKEND_PRECONDITION:
            if expected_generation is None:
                raise ValueError(
                    f"{kind!r} previews must carry expected_generation — without it the "
                    "backend cannot reject a stale confirm"
                )
            if old_values is not None or reread_endpoint is not None:
                raise ValueError(f"{kind!r} previews do not carry old_values")
        elif strategy == REREAD_OLD_VALUES:
            # Claiming the *other* strategy is reported before anything missing:
            # it says the caller has the wrong model of the kind, which is the
            # more useful complaint.
            if expected_generation is not None:
                raise ValueError(f"{kind!r} previews carry no backend precondition")
            if old_values is None:
                raise ValueError(
                    f"{kind!r} previews must carry old_values — they are what apply_change "
                    "re-reads and compares before writing"
                )
            if not reread_endpoint:
                raise ValueError(
                    f"{kind!r} previews must carry reread_endpoint — without it apply_change "
                    "has nowhere to re-read the previewed values from"
                )
        else:
            if expected_generation is not None or old_values is not None or reread_endpoint:
                raise ValueError(
                    f"{kind!r} previews are defended by their TTL alone; they carry neither "
                    "expected_generation nor old_values"
                )

        now = self.clock()
        preview = Preview(
            token=f"{kind}-{secrets.token_urlsafe(12)}",
            kind=kind,
            target=target,
            method=method,
            endpoint=endpoint,
            # Deep copies: `params` nests (a tags edit carries a `fields` dict)
            # and the record must keep what was previewed, not follow a caller
            # that reuses its dict for the next preview.
            params=deepcopy(dict(params or {})),
            expected_generation=expected_generation,
            old_values=None if old_values is None else deepcopy(dict(old_values)),
            reread_endpoint=reread_endpoint,
            requested_item_ids=None if requested_item_ids is None else tuple(requested_item_ids),
            summary=deepcopy(dict(summary or {})),
            created_at=now,
            expires_at=now + ttl_for(kind),
        )
        with self._lock:
            self._purge_expired(now)
            self._records[preview.token] = preview
        return preview

    def peek(self, token: str) -> Preview:
        """Look a token up without burning it. Raises like `take`."""
        with self._lock:
            return self._live(token, self.clock())

    def take(self, token: str) -> Preview:
        """Burn a token and return its record — the one path to an apply.

        Raises `PreviewError` with `ALREADY_APPLIED`, `EXPIRED` or
        `UNKNOWN_TOKEN`, each carrying its own instruction.

        The burn is recorded as `ALREADY_APPLIED` *before* the write is
        attempted, which is the safe default: a process that dies mid-write must
        not tell the next caller that nothing happened. `mark_failed` corrects it
        once the apply is known to have failed.
        """
        with self._lock:
            preview = self._live(token, self.clock())
            del self._records[token]
            self._burn(token, ALREADY_APPLIED)
            return preview

    def mark_failed(self, token: str) -> None:
        """Re-label a burned token as spent on a *failed* apply.

        Called by `apply_change` once the handler has answered `failed`. Without
        it, retrying the token answers "has already been applied" — asserting a
        write that a drift refusal or an unreachable backend never made.
        """
        with self._lock:
            if self._burned.get(token) == ALREADY_APPLIED:
                self._burn(token, APPLY_FAILED)

    def purge_expired(self) -> int:
        """Drop expired records; returns how many went. Mint does this too."""
        with self._lock:
            return self._purge_expired(self.clock())

    def describe(self, preview: Preview) -> dict[str, Any]:
        """The agent-facing half of a preview response."""
        described = {
            "preview_token": preview.token,
            "kind": preview.kind,
            "target": preview.target,
            "expires_in_seconds": max(0, int(preview.expires_at - self.clock())),
            "guarantee": preview.guarantee,
        }
        if preview.summary:
            described["changes"] = preview.summary
        return described

    # -- internals (all called with the lock held) --------------------------

    def _live(self, token: str, now: float) -> Preview:
        preview = self._records.get(token)
        if preview is None:
            raise self._gone(token)
        if preview.expired(now):
            del self._records[token]
            self._burn(token, EXPIRED)
            raise self._expired(preview)
        return preview

    def _gone(self, token: str) -> PreviewError:
        reason = self._burned.get(token)
        if reason == ALREADY_APPLIED:
            return PreviewError(
                ALREADY_APPLIED,
                f"preview token {token} has already been applied — each token commits "
                "exactly once; re-run the preview if you want to repeat the change",
            )
        if reason == APPLY_FAILED:
            return PreviewError(
                APPLY_FAILED,
                f"preview token {token} was already spent on an apply that failed — the "
                "failure said what went wrong and tokens commit exactly once either way; "
                "re-run the preview and decide again on the current state",
            )
        if reason == EXPIRED:
            return PreviewError(
                EXPIRED,
                f"preview token {token} expired before it was applied — nothing was "
                "written; re-run the preview to get a fresh one",
            )
        return PreviewError(
            UNKNOWN_TOKEN,
            f"no such preview token {token} — tokens do not survive a restart of the MCP "
            "server; re-run the preview",
        )

    def _expired(self, preview: Preview) -> PreviewError:
        return PreviewError(
            EXPIRED,
            f"preview {preview.token} for {preview.target} expired after "
            f"{int(preview.ttl / 60)} minutes — nothing was written; re-run the preview, "
            "the library may have changed in the meantime",
        )

    def _burn(self, token: str, reason: str) -> None:
        self._burned[token] = reason
        self._burned.move_to_end(token)
        while len(self._burned) > BURNED_HISTORY:
            self._burned.popitem(last=False)

    def _purge_expired(self, now: float) -> int:
        stale = [token for token, preview in self._records.items() if preview.expired(now)]
        for token in stale:
            del self._records[token]
            self._burn(token, EXPIRED)
        return len(stale)


_DEFAULT_STORE = PreviewStore()


def default_store() -> PreviewStore:
    """The process-wide store. Preview producers and `apply_change` live in
    different tool modules and must share one table."""
    return _DEFAULT_STORE
