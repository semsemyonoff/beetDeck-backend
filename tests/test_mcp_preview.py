"""Preview-token store: minting, single use, TTL, and the three freshness
strategies.

The staleness paths are the point of this module, so they are tested where they
actually live: value drift as a pure comparison, and the identification
precondition against the **real** confirm endpoint in-process — the backend is
what enforces that one, so a mock would prove nothing.
"""

import pytest

from beetdeck import state
from beetdeck_mcp.client import WRITE_KINDS, ApplyResult
from beetdeck_mcp.preview import (
    ALREADY_APPLIED,
    BACKEND_PRECONDITION,
    DEFAULT_TTL,
    DRIFT,
    EXPIRED,
    FRESHNESS_BY_KIND,
    GUARANTEE,
    REREAD_OLD_VALUES,
    SHORT_TTL,
    STALE_GENERATION,
    TTL_ONLY,
    UNKNOWN_TOKEN,
    PreviewError,
    PreviewStore,
    check_old_values,
    default_store,
    freshness_for,
    precondition_failed,
    ttl_for,
)
from tests.conftest import insert_album


class FakeClock:
    """A monotonic clock the tests move by hand — a 15-minute TTL is not
    something to sleep through."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def store(clock):
    return PreviewStore(clock=clock)


def mint_identify(store, *, album_id=1, generation=7, index=0):
    return store.mint(
        "identify",
        target=f"album {album_id}",
        method="POST",
        endpoint=f"/api/album/{album_id}/confirm",
        params={"candidate_index": index},
        expected_generation=generation,
    )


def mint_tags(store, *, item_id=5, album_id=1, old_values=None):
    return store.mint(
        "tags",
        target=f"item {item_id}",
        method="PATCH",
        endpoint=f"/api/items/{item_id}/tags",
        params={"fields": {"title": "New Title"}},
        old_values={"title": "Old Title"} if old_values is None else old_values,
        reread_endpoint=f"/api/album/{album_id}/track/{item_id}/tags",
    )


def mint_cover(store, *, album_id=1):
    return store.mint(
        "cover",
        target=f"album {album_id}",
        method="POST",
        endpoint=f"/api/album/{album_id}/cover/confirm",
    )


# ---------------------------------------------------------------------------
# the guarantee table
# ---------------------------------------------------------------------------


class TestFreshnessTable:
    def test_every_write_kind_has_a_named_strategy(self):
        assert set(FRESHNESS_BY_KIND) == set(WRITE_KINDS)

    def test_every_strategy_has_agent_facing_wording(self):
        assert set(GUARANTEE) == {BACKEND_PRECONDITION, REREAD_OLD_VALUES, TTL_ONLY}
        assert all(text for text in GUARANTEE.values())

    @pytest.mark.parametrize("kind", ["identify", "items_identify"])
    def test_identification_is_backend_enforced(self, kind):
        assert freshness_for(kind) == BACKEND_PRECONDITION
        assert ttl_for(kind) == DEFAULT_TTL

    @pytest.mark.parametrize("kind", ["tags", "genres"])
    def test_tags_and_genres_re_read(self, kind):
        assert freshness_for(kind) == REREAD_OLD_VALUES
        assert ttl_for(kind) == DEFAULT_TTL

    @pytest.mark.parametrize("kind", ["cover", "lyrics"])
    def test_cover_and_lyrics_have_the_ttl_only_and_a_short_one(self, kind):
        assert freshness_for(kind) == TTL_ONLY
        assert ttl_for(kind) == SHORT_TTL
        assert "wins silently" in GUARANTEE[TTL_ONLY]

    @pytest.mark.parametrize("kind", ["", "unknown", "identity"])
    def test_an_unknown_kind_is_a_programming_error(self, kind):
        with pytest.raises(ValueError, match="unknown preview kind"):
            freshness_for(kind)
        with pytest.raises(ValueError, match="unknown preview kind"):
            ttl_for(kind)


# ---------------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------------


class TestMint:
    def test_records_everything_apply_change_needs(self, store, clock):
        preview = mint_identify(store, album_id=12, generation=3, index=1)
        assert preview.kind == "identify"
        assert preview.token.startswith("identify-")
        assert preview.endpoint == "/api/album/12/confirm"
        assert preview.method == "POST"
        assert preview.expected_generation == 3
        assert preview.expires_at == clock.now + DEFAULT_TTL
        assert len(store) == 1

    def test_tokens_are_unique(self, store):
        tokens = {mint_identify(store).token for _ in range(20)}
        assert len(tokens) == 20

    def test_the_confirm_body_carries_the_precondition(self, store):
        preview = mint_identify(store, generation=9, index=2)
        assert preview.request_json() == {"candidate_index": 2, "expected_generation": 9}

    def test_other_kinds_do_not_send_a_precondition(self, store):
        assert mint_tags(store).request_json() == {"fields": {"title": "New Title"}}

    def test_a_bodyless_confirm_sends_no_body(self, store):
        assert mint_cover(store).request_json() is None

    def test_params_are_copied_not_aliased(self, store):
        fields = {"title": "New"}
        preview = store.mint(
            "tags",
            target="item 1",
            method="PATCH",
            endpoint="/api/items/1/tags",
            params={"fields": fields},
            old_values={"title": "Old"},
            reread_endpoint="/api/album/1/track/1/tags",
        )
        fields["title"] = "Mutated"
        assert preview.params["fields"]["title"] == "New"

    def test_identification_without_a_generation_is_refused(self, store):
        with pytest.raises(ValueError, match="must carry expected_generation"):
            store.mint("identify", target="album 1", method="POST", endpoint="/api/album/1/confirm")

    def test_tags_without_old_values_are_refused(self, store):
        with pytest.raises(ValueError, match="must carry old_values"):
            store.mint("tags", target="item 1", method="PATCH", endpoint="/api/items/1/tags")

    def test_tags_without_somewhere_to_re_read_from_are_refused(self, store):
        """Old values with no read path is the same downgrade as no old values."""
        with pytest.raises(ValueError, match="must carry reread_endpoint"):
            store.mint(
                "tags",
                target="item 1",
                method="PATCH",
                endpoint="/api/items/1/tags",
                old_values={"title": "Old"},
            )

    def test_a_backend_enforced_kind_may_not_claim_a_re_read(self, store):
        with pytest.raises(ValueError, match="do not carry old_values"):
            store.mint(
                "identify",
                target="album 1",
                method="POST",
                endpoint="/api/album/1/confirm",
                expected_generation=1,
                reread_endpoint="/api/album/1",
            )

    def test_a_ttl_only_kind_may_not_claim_a_re_read(self, store):
        with pytest.raises(ValueError, match="defended by their TTL alone"):
            store.mint(
                "cover",
                target="album 1",
                method="POST",
                endpoint="/api/album/1/cover/confirm",
                reread_endpoint="/api/album/1",
            )

    def test_a_ttl_only_kind_may_not_claim_a_stronger_defence(self, store):
        with pytest.raises(ValueError, match="defended by their TTL alone"):
            store.mint(
                "cover",
                target="album 1",
                method="POST",
                endpoint="/api/album/1/cover/confirm",
                old_values={"art": "x"},
            )

    def test_a_kind_may_not_claim_the_other_strategy(self, store):
        with pytest.raises(ValueError, match="do not carry old_values"):
            store.mint(
                "identify",
                target="album 1",
                method="POST",
                endpoint="/api/album/1/confirm",
                expected_generation=1,
                old_values={"title": "Old"},
            )
        with pytest.raises(ValueError, match="carry no backend precondition"):
            store.mint(
                "tags",
                target="item 1",
                method="PATCH",
                endpoint="/api/items/1/tags",
                expected_generation=1,
                old_values={"title": "Old"},
            )

    def test_an_unknown_kind_never_reaches_the_table(self, store):
        with pytest.raises(ValueError, match="unknown preview kind"):
            store.mint("release", target="album 1", method="POST", endpoint="/x")
        assert len(store) == 0

    def test_describe_is_the_agent_facing_shape(self, store):
        preview = store.mint(
            "genres",
            target="album 4",
            method="POST",
            endpoint="/api/album/4/genre/save",
            params={"genre": "Ambient"},
            old_values={"genre": "Rock"},
            reread_endpoint="/api/album/4",
            summary={"genre": {"old": "Rock", "new": "Ambient"}},
        )
        described = store.describe(preview)
        assert described["preview_token"] == preview.token
        assert described["kind"] == "genres"
        assert described["target"] == "album 4"
        assert described["expires_in_seconds"] == int(DEFAULT_TTL)
        assert described["guarantee"] == GUARANTEE[REREAD_OLD_VALUES]
        assert described["changes"] == {"genre": {"old": "Rock", "new": "Ambient"}}

    def test_describe_of_an_expired_token_reports_zero_not_a_negative(self, store, clock):
        preview = mint_cover(store)
        clock.advance(SHORT_TTL + 60)
        assert store.describe(preview)["expires_in_seconds"] == 0


# ---------------------------------------------------------------------------
# single use and expiry
# ---------------------------------------------------------------------------


class TestTake:
    def test_mint_then_apply(self, store):
        preview = mint_identify(store, album_id=8, generation=2)
        taken = store.take(preview.token)
        assert taken == preview
        assert len(store) == 0

    def test_peek_does_not_burn(self, store):
        preview = mint_tags(store)
        assert store.peek(preview.token) == preview
        assert store.take(preview.token) == preview

    def test_a_second_apply_is_rejected_and_says_why(self, store):
        preview = mint_identify(store)
        store.take(preview.token)
        with pytest.raises(PreviewError) as excinfo:
            store.take(preview.token)
        assert excinfo.value.reason == ALREADY_APPLIED
        assert "already been applied" in str(excinfo.value)
        assert excinfo.value.stale is False

    def test_an_unknown_token_is_not_reported_as_already_applied(self, store):
        with pytest.raises(PreviewError) as excinfo:
            store.take("identify-nope")
        assert excinfo.value.reason == UNKNOWN_TOKEN
        assert "no such preview token" in str(excinfo.value)

    def test_an_expired_token_is_rejected_with_its_own_message(self, store, clock):
        preview = mint_tags(store)
        clock.advance(DEFAULT_TTL + 1)
        with pytest.raises(PreviewError) as excinfo:
            store.take(preview.token)
        assert excinfo.value.reason == EXPIRED
        assert "expired after 15 minutes" in str(excinfo.value)
        assert "nothing was written" in str(excinfo.value)

    def test_a_token_is_still_applicable_one_second_before_expiry(self, store, clock):
        preview = mint_tags(store)
        clock.advance(DEFAULT_TTL - 1)
        assert store.take(preview.token) == preview

    def test_retrying_an_expired_token_keeps_saying_expired(self, store, clock):
        preview = mint_tags(store)
        clock.advance(DEFAULT_TTL + 1)
        with pytest.raises(PreviewError):
            store.take(preview.token)
        with pytest.raises(PreviewError) as excinfo:
            store.take(preview.token)
        assert excinfo.value.reason == EXPIRED

    def test_cover_and_lyrics_expire_after_five_minutes(self, store, clock):
        cover = mint_cover(store)
        lyrics = store.mint(
            "lyrics",
            target="album 1",
            method="POST",
            endpoint="/api/album/1/lyrics/confirm",
            params={"item_ids": [3]},
            requested_item_ids=[3],
        )
        clock.advance(SHORT_TTL - 1)
        assert store.peek(cover.token) is cover
        clock.advance(2)
        for preview in (cover, lyrics):
            with pytest.raises(PreviewError) as excinfo:
                store.take(preview.token)
            assert excinfo.value.reason == EXPIRED
            assert "expired after 5 minutes" in str(excinfo.value)

    def test_a_long_lived_kind_outlives_the_short_ttl(self, store, clock):
        preview = mint_identify(store)
        clock.advance(SHORT_TTL + 60)
        assert store.take(preview.token) == preview

    def test_expired_records_are_purged(self, store, clock):
        mint_cover(store, album_id=1)
        mint_cover(store, album_id=2)
        survivor = mint_identify(store)
        clock.advance(SHORT_TTL + 1)
        assert store.purge_expired() == 2
        assert len(store) == 1
        assert store.peek(survivor.token) == survivor

    def test_minting_purges_expired_records(self, store, clock):
        mint_cover(store)
        clock.advance(SHORT_TTL + 1)
        mint_identify(store)
        assert len(store) == 1

    def test_the_lyrics_requested_ids_survive_the_round_trip(self, store):
        preview = store.mint(
            "lyrics",
            target="album 1",
            method="POST",
            endpoint="/api/album/1/lyrics/confirm",
            params={"item_ids": [3, 4]},
            requested_item_ids=[3, 4],
        )
        assert store.take(preview.token).requested_item_ids == (3, 4)

    def test_the_burned_history_is_bounded(self, store):
        """It exists to explain a second apply, so it must not grow forever."""
        from beetdeck_mcp.preview import BURNED_HISTORY

        first = mint_cover(store)
        store.take(first.token)
        for _ in range(BURNED_HISTORY):
            store.take(mint_cover(store).token)

        with pytest.raises(PreviewError) as excinfo:
            store.take(first.token)
        assert excinfo.value.reason == UNKNOWN_TOKEN


class TestDefaultStore:
    def test_is_a_single_process_wide_table(self):
        assert default_store() is default_store()


# ---------------------------------------------------------------------------
# strategy 2 — re-read and compare
# ---------------------------------------------------------------------------


class TestOldValueDrift:
    def test_unchanged_values_pass(self, store):
        preview = mint_tags(store, old_values={"title": "Old Title", "artist": "A"})
        check_old_values(preview, {"title": "Old Title", "artist": "A", "album": "irrelevant"})

    def test_a_changed_value_is_refused_and_named(self, store):
        preview = mint_tags(store, old_values={"title": "Old Title", "artist": "A"})
        with pytest.raises(PreviewError) as excinfo:
            check_old_values(preview, {"title": "Someone Else Wrote This", "artist": "A"})
        error = excinfo.value
        assert error.reason == DRIFT
        assert error.stale is True
        assert "title" in str(error)
        assert "Old Title" in str(error) and "Someone Else Wrote This" in str(error)
        assert "artist" not in str(error)
        assert "nothing was written" in str(error)

    def test_a_field_that_disappeared_counts_as_drift(self, store):
        preview = mint_tags(store, old_values={"comments": "note"})
        with pytest.raises(PreviewError) as excinfo:
            check_old_values(preview, {})
        assert excinfo.value.reason == DRIFT

    def test_an_unset_field_reads_the_same_whether_none_or_empty(self, store):
        """An empty value clears the field, so `None` and `""` are one state."""
        preview = mint_tags(store, old_values={"comments": None})
        check_old_values(preview, {"comments": ""})

    def test_list_values_compare_element_wise(self, store):
        preview = mint_tags(store, old_values={"genres": ["Rock", "Pop"]})
        check_old_values(preview, {"genres": ("Rock", "Pop")})
        with pytest.raises(PreviewError):
            check_old_values(preview, {"genres": ["Pop", "Rock"]})

    def test_every_drifted_field_is_reported_not_just_the_first(self, store):
        preview = mint_tags(store, old_values={"title": "A", "artist": "B"})
        with pytest.raises(PreviewError) as excinfo:
            check_old_values(preview, {"title": "X", "artist": "Y"})
        assert "title" in str(excinfo.value) and "artist" in str(excinfo.value)

    def test_calling_it_for_another_strategy_is_a_programming_error(self, store):
        with pytest.raises(ValueError, match="not by re-reading old values"):
            check_old_values(mint_identify(store), {})

    def test_the_refusal_is_the_agent_facing_failed_shape(self, store):
        preview = mint_tags(store)
        with pytest.raises(PreviewError) as excinfo:
            check_old_values(preview, {"title": "Changed"})
        result = excinfo.value.as_result()
        assert result.status == "failed"
        assert result.stale is True
        assert result.as_dict()["detail"] == [str(excinfo.value)]


# ---------------------------------------------------------------------------
# strategy 1 — the backend precondition
# ---------------------------------------------------------------------------


class TestBackendPrecondition:
    def test_the_refusal_names_the_target_and_the_way_out(self, store):
        preview = mint_identify(store, album_id=12)
        error = precondition_failed(preview)
        assert error.reason == STALE_GENERATION
        assert error.stale is True
        assert "album 12" in str(error)
        assert "re-run or already applied" in str(error)
        assert "identify again" in str(error)

    def test_the_backend_message_is_carried_through(self, store):
        result = ApplyResult("failed", ("HTTP 409: results changed",), stale=True)
        error = precondition_failed(mint_identify(store), result)
        assert "HTTP 409: results changed" in str(error)

    def test_calling_it_for_another_strategy_is_a_programming_error(self, store):
        with pytest.raises(ValueError, match="carry no backend precondition"):
            precondition_failed(mint_tags(store))

    def test_a_bumped_generation_is_rejected_by_the_real_backend(
        self, store, beetdeck_client, db_path
    ):
        """The whole point of the identification kinds: the refusal comes from
        inside `state.identify_lock`, not from a check we do afterwards."""
        album_id = insert_album(db_path)
        generation = state.next_match_generation()
        state.identify_tasks[f"album_{album_id}"] = {
            "status": "done",
            "match_generation": generation,
            "_matches": ["candidate"],
        }
        preview = mint_identify(store, album_id=album_id, generation=generation)

        # A re-identify rebuilds the candidate list and bumps the counter.
        state.identify_tasks[f"album_{album_id}"]["match_generation"] = (
            state.next_match_generation()
        )

        taken = store.take(preview.token)
        result = beetdeck_client.write(
            taken.kind, taken.method, taken.endpoint, json=taken.request_json()
        )
        assert result.status == "failed"
        assert result.stale is True

        error = precondition_failed(taken, result)
        assert error.reason == STALE_GENERATION
        assert "changed since the preview" in str(error)

    def test_a_matching_generation_passes_the_precondition(self, store, beetdeck_client, db_path):
        """Index 5 out of a one-candidate list: reaching `Invalid candidate
        index` proves the precondition let the confirm through."""
        album_id = insert_album(db_path)
        generation = state.next_match_generation()
        state.identify_tasks[f"album_{album_id}"] = {
            "status": "done",
            "match_generation": generation,
            "_matches": ["candidate"],
        }
        preview = mint_identify(store, album_id=album_id, generation=generation, index=5)

        taken = store.take(preview.token)
        result = beetdeck_client.write(
            taken.kind, taken.method, taken.endpoint, json=taken.request_json()
        )
        assert result.stale is False
        assert "Invalid candidate index" in result.detail[0]

    def test_a_purged_task_is_reported_as_stale_not_as_missing(
        self, store, beetdeck_client, db_path
    ):
        """Task 3 contract: with the precondition sent, a vanished task is a
        mismatch (409), which is the path a purged `items_*` task takes."""
        album_id = insert_album(db_path)
        preview = mint_identify(store, album_id=album_id, generation=11)
        taken = store.take(preview.token)
        result = beetdeck_client.write(
            taken.kind, taken.method, taken.endpoint, json=taken.request_json()
        )
        assert result.stale is True


# ---------------------------------------------------------------------------
# the refusals must be distinguishable
# ---------------------------------------------------------------------------


class TestRefusalsAreDistinct:
    def _refusals(self, store, clock):
        expired = mint_tags(store)
        applied = mint_tags(store)
        store.take(applied.token)
        drifting = mint_tags(store)
        clock.advance(DEFAULT_TTL + 1)

        errors = []
        for token in (expired.token, applied.token, "tags-unknown"):
            with pytest.raises(PreviewError) as excinfo:
                store.take(token)
            errors.append(excinfo.value)
        with pytest.raises(PreviewError) as excinfo:
            check_old_values(drifting, {"title": "Changed"})
        errors.append(excinfo.value)
        errors.append(precondition_failed(mint_identify(store)))
        return errors

    def test_each_reason_appears_once(self, store, clock):
        reasons = [error.reason for error in self._refusals(store, clock)]
        assert sorted(reasons) == sorted(
            [EXPIRED, ALREADY_APPLIED, UNKNOWN_TOKEN, DRIFT, STALE_GENERATION]
        )

    def test_each_message_is_its_own(self, store, clock):
        messages = [str(error) for error in self._refusals(store, clock)]
        assert len(set(messages)) == len(messages)

    def test_only_the_library_moving_is_flagged_stale(self, store, clock):
        by_reason = {error.reason: error for error in self._refusals(store, clock)}
        assert by_reason[DRIFT].stale is True
        assert by_reason[STALE_GENERATION].stale is True
        assert by_reason[EXPIRED].stale is False
        assert by_reason[ALREADY_APPLIED].stale is False
        assert by_reason[UNKNOWN_TOKEN].stale is False

    def test_every_refusal_says_what_to_do_next(self, store, clock):
        for error in self._refusals(store, clock):
            message = str(error).lower()
            assert "re-run the preview" in message or "identify again" in message
