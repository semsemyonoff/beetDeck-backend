# beets upgrade notes

Per-release findings from bumping the beets pin in `pyproject.toml`. Newest first.
Add an entry when you bump the version; the current pin itself lives in
`pyproject.toml`, not here.

## 2.13.1

Packaging only upstream — the sdist was missing its bundled man pages. Nothing
for this app; the version is folded into the 2.13.0 notes below.

## 2.13.0

No API signature changes: `autotag.tag_album`, `plugins.{find_plugins,
load_plugins,send}`, `library.Library.__init__`, `util.{bytestring_path,move,
unique_path}` and `art.embed_album` are all unchanged, `Item._fields` /
`_media_fields` / `_media_tag_fields` are byte-identical to 2.12.0, and every
literal `scan_progress.py` matches still appears verbatim in
`beet -v import -A -C` / `beet -v update -M` output.

Two behaviour changes did need work, plus one deployment step.

### Deployment step: run the migrations single-process, before traffic

2.13.0 adds two `Item` migrations (`RemoveInheritedArtpathMigration`,
`InstrumentalLyricsInFlexFieldMigration`). Migrations run inside **any**
`beets.library.Library(...)` construction, not just the `beet` CLI — and
`_init_beets()` builds a fresh `Library` per call, outside its init lock. Two
concurrent first opens (two request threads, or a request racing the
`beet import` subprocess) both try to record the same migration and one dies
with `sqlite3.IntegrityError`. Upstream master is still unfixed.

So after the deploy, before letting traffic or a rescan in:

```
dwe cmd backend.beet -- stats     # single process, completes both migrations
```

It is a one-shot window: once the rows land in the `migrations` table, later
opens skip straight past.

Each migration also copies the whole DB to
`/data/beets/library.db-before-items-<name>.bak` first — unconditionally, even
when it matches no rows — and nothing ever prunes them. Two files on an existing
library, eleven on a fresh one. Delete them once the app is verified, or set
`create_backup_before_migrations: no` (top-level) in the beets config. Kept on
by default here because the lyrics migration mutates data.

### Instrumental lyrics are a third state now

`Lyrics.handle_instrumental()` blanks `.text` and sets `.instrumental` when a
source answers `[Instrumental]`, and the migration rewrites existing rows to
empty lyrics plus a `lyrics_instrumental` flex attribute. Left alone, our fetch
endpoints would report such a track as `found: false` — indistinguishable from a
network miss — and `has_lyrics` would silently flip to false for already-tagged
instrumentals.

`beetdeck/routes/lyrics.py` therefore treats it as its own outcome: `GET
…/lyrics` returns `instrumental`, both fetch endpoints return
`new_instrumental` alongside `found: false` and still stash the result, and both
confirm paths plus the manual save write or clear the flag. Confirming is what
makes the migration stick — it writes empty lyrics through to the file tag,
where the `[Instrumental]` marker otherwise survives and a later `beet update`
reads it straight back into the library.

### lastgenre output drifts (no action taken)

`lastgenre.aliases` is new and **on by default**: genre spellings are normalized
before whitelist filtering. Both `/genre` endpoints sit on that path. Expect
renamed spellings (`Synthpop`→`Synth-Pop`, `Shoegazer`→`Shoegaze`,
`Dixieland Jazz`→`Dixieland`, …) and six previously-valid tags now dropped with
no alias to catch them: `dance`, `psychedelic trance`, `2-step garage`,
`rhythm and blues`, `industrial music`, `noise music`. Only `dance` is a common
Last.fm tag; the rest are reachable under other spellings. Existing rows are not
rewritten, so the library accumulates mixed spellings — cosmetic, since nothing
groups or filters on the exact string. Left at the default deliberately. Note if
you ever want to override it: an inline `aliases` mapping **replaces** the
built-in table rather than extending it.

### Smaller notes

- beets no longer sets `propagate = False` on its logger at import (that moved
  into the CLI-only `_bootstrap_logging`), so our `setLevel(DEBUG)` in
  `_init_beets` now routes beets DEBUG records through the root handler. Same
  stream, same volume, different line format.
- `RemoveInheritedArtpathMigration` deletes every `item_attributes` row with
  `key='artpath'`. We never read item-level artpath, so nothing breaks; the one
  visible effect is that `GET …/tags` stops leaking a stale `artpath` entry.
- `test_beets_version_pinned` now compares `BEETS_VERSION` against
  `beets.__version__` instead of a second literal. The old form could not fail,
  which is how 2.13.0 and 2.13.1 both went by without the parser being rechecked.
- Available but not adopted: `fetchart.fetch_for_asis` (would fetch art during
  our `-A` import, but nothing filters out iTunes' fallback candidate, so covers
  would land unreviewed) and the `lrcmux` lyrics backend (no-auth aggregator,
  supports synced LRC — a plausible addition next to `lrclib`).

## 2.12.0

No breaking API changes for this app.

- The lyrics plugin was reworked — synced lyrics are now written to the `SYLT` ID3
  frame, and the standalone `LyricsSession` was folded into
  `beetsplug._utils.requests`. The `Lyrics` result object returned by
  `lyrics_plugin.find_lyrics(item)` still exposes every attribute we read
  (`text`, `full_text`, `synced`, `backend`, `url`, `language`,
  `translation_language`), so `beetdeck/routes/lyrics.py` is unchanged.
- `art.embed_album` signature unchanged.
- Upstream fixes we benefit from: fetchart catches `OSError` instead of crashing
  on a locked art file; lyrics HTTP requests rate-limit and back off on `429`.

## 2.11.0

No breaking API changes for this app — verified against the modules we import:
`autotag.tag_album`, art embedding, `plugins`, `library.Library`,
`util.{bytestring_path,move,unique_path}`, `config`.

- New `subtitle` media-tag field (ID3 `TIT3`) added to `Item`, so it is included
  in `_ROLLBACK_FIELDS` (`beetdeck/routes/items.py`).
- Cover embedding moved to `beetsplug._utils.art.embed_album`; the old `beets.art`
  shim is deprecated and slated for removal in beets 3.0.0. Cover tests patch
  `beetsplug._utils.art.embed_album`.
