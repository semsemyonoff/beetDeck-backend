"""`apply_change` — the only tool in this server that writes.

Every mutating capability mints a preview token; this is where a token is spent.
Concentrating the writes in one tool is what makes `BEETDECK_MCP_READONLY`
meaningful: a single check disables every write while leaving the whole preview
surface working, so an agent can still sweep the library and report what it
*would* change.

Committing a token is three steps, and each one belongs to a different owner:

1. the store burns the token (single use, TTL) — `preview.py`;
2. the kind's handler applies its freshness strategy and sends the request —
   here;
3. the client maps the response onto `ok`/`partial`/`failed` — `client.py`.

`_HANDLERS` is keyed by preview kind; later tool tasks add their kind here, so a
kind that can be previewed but not committed is visible as a missing entry rather
than as a mystery at apply time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beetdeck_mcp.client import ApplyResult, BeetdeckClient
from beetdeck_mcp.preview import Preview, PreviewError, PreviewStore, default_store
from beetdeck_mcp.preview import check_old_values as _check_old_values
from beetdeck_mcp.preview import precondition_failed as _precondition_failed
from beetdeck_mcp.tools import client_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server import MCPServer

    from beetdeck_mcp.config import Config

TOOL_NAMES = ("apply_change",)

#: `cover`/`lyrics` confirms embed into every track of the album one file at a
#: time, so the write scales with the album the way the fetch does — the 30 s
#: client default would abort a long one mid-embed. See `media.FETCH_TIMEOUT`.
EMBED_TIMEOUT = 300.0

READONLY_REFUSAL = (
    "BEETDECK_MCP_READONLY is set on this server — apply_change is disabled and nothing was "
    "written. Previews still work, so you can report what would change; the preview token "
    "remains valid until it expires."
)


def _apply_backend_precondition(client: BeetdeckClient, preview: Preview) -> ApplyResult:
    """Commit a kind whose freshness the backend enforces (the identify kinds).

    `request_json()` adds `expected_generation`, which the confirm handler
    compares inside `state.identify_lock` — so a re-identify or a competing
    confirm is rejected there, atomically, and comes back as a `409`.
    """
    result = client.write(
        preview.kind,
        preview.method,
        preview.endpoint,
        json=preview.request_json(),
    )
    if result.stale:
        return _precondition_failed(preview, result).as_result()
    return result


def _apply_after_reread(client: BeetdeckClient, preview: Preview) -> ApplyResult:
    """Commit a kind whose freshness only this process can check (`tags`, `genres`).

    The previewed fields are read again from `preview.reread_endpoint` and
    compared before the write. A read that fails is a refusal, not a write: the
    guarantee the token was minted with is the one thing that must not quietly
    degrade to "wrote it anyway".
    """
    current = client.get(preview.reread_endpoint or "")
    if not current.ok:
        return ApplyResult(
            "failed",
            (
                f"the current values of {preview.target} could not be re-read before writing "
                f"({current.message}) — nothing was written, so the previewed values could "
                "not be confirmed; try again",
            ),
        )

    try:
        _check_old_values(preview, current.data)
    except PreviewError as refusal:
        return refusal.as_result()

    return client.write(
        preview.kind,
        preview.method,
        preview.endpoint,
        json=preview.request_json(),
    )


def _apply_ttl_only(client: BeetdeckClient, preview: Preview) -> ApplyResult:
    """Commit a kind nothing but the clock defends (`cover`, `lyrics`).

    There is no check to make: the backend's fetch stash has no identity and no
    read endpoint, so a competing fetch cannot be detected from here at all. The
    honest implementation is to send the request and let the normalizer report
    what came back — including the ids the lyrics confirm silently skipped, which
    is why `requested_item_ids` is threaded through.
    """
    return client.write(
        preview.kind,
        preview.method,
        preview.endpoint,
        json=preview.request_json(),
        timeout=EMBED_TIMEOUT,
        requested_item_ids=preview.requested_item_ids,
    )


_HANDLERS: Mapping[str, Callable[[BeetdeckClient, Preview], ApplyResult]] = {
    "identify": _apply_backend_precondition,
    "items_identify": _apply_backend_precondition,
    "tags": _apply_after_reread,
    "genres": _apply_after_reread,
    "cover": _apply_ttl_only,
    "lyrics": _apply_ttl_only,
}


def build_tools(
    client: BeetdeckClient,
    store: PreviewStore,
    *,
    readonly: bool = False,
) -> tuple[Callable[..., dict[str, Any]], ...]:
    """`apply_change` bound to `client`, `store` and the read-only flag."""

    def apply_change(preview_token: str) -> dict[str, Any]:
        """Commit a previewed change. This is the only tool that writes.

        Pass the `preview_token` returned by a `preview_*` tool. Tokens are
        single-use and time-limited, and a refusal always says which it was:
        expired, already applied, the previewed values changed, or identification
        was re-run. In every one of those cases **nothing was written** — re-run
        the preview and decide again on the current state.

        The result is `{status, detail}` where `status` is `ok`, `partial` (the
        database was written but not every file) or `failed`.
        """
        if readonly:
            return ApplyResult("failed", (READONLY_REFUSAL,)).as_dict()

        try:
            preview = store.take(preview_token)
        except PreviewError as refusal:
            return refusal.as_result().as_dict()

        handler = _HANDLERS.get(preview.kind)
        if handler is None:
            return ApplyResult(
                "failed",
                (
                    f"apply_change cannot commit a {preview.kind!r} preview — this server "
                    "build has no handler for that kind; nothing was written",
                ),
            ).as_dict()

        result = handler(client, preview)
        if result.status == "failed":
            # The token stays spent — it is single-use either way — but a retry
            # must not be told the change "has already been applied" when the
            # handler refused on drift or never reached the backend.
            store.mark_failed(preview.token)
        return {**result.as_dict(), "kind": preview.kind, "target": preview.target}

    return (apply_change,)


def register(server: "MCPServer", config: "Config") -> None:
    for tool in build_tools(client_for(config), default_store(), readonly=config.readonly):
        server.add_tool(tool)
