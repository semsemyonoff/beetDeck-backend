# beets upgrade notes

Per-release findings from bumping the beets pin in `pyproject.toml`. Newest first.
Add an entry when you bump the version; the current pin itself lives in
`pyproject.toml`, not here.

## 2.14.1

Compatibility-first upgrade from 2.13.1. The exact GitHub release-tarball URL
shape is retained for Renovate. No new UI, plugin/config changes, or adoption of
the new duplicate `upgrade` action: `import.duplicate_action: remove` remains the
deployment contract.

### Source comparison and required adaptation

Compared the extracted **v2.13.1 and v2.14.1 release sources**, not only release
notes: [tag-to-tag comparison](https://github.com/beetbox/beets/compare/v2.13.1...v2.14.1).
Paths below refer to those tagged trees.

- **Identification needs an API cutover.** In `beets/autotag/match.py`,
  `tag_album(items, ...) -> (artist, album, Proposal)` becomes
  `tag_album(Source, ...) -> Proposal`. Both album identify and untagged-item
  identify now build `autotag.Source.from_items(items)` and consume the returned
  proposal directly. `Source` retains the original `Item` objects, so candidate
  mappings still refer to the items subsequently confirmed. Mock fixtures were
  migrated and real autotag contract regressions added; response/stash/confirm
  contracts are unchanged.
- **Lyrics needs coverage, not a route rewrite.** `beets/util/lyrics.py` now accepts
  two- and three-digit fractional LRC timestamps and pads fractions when deriving
  SYLT milliseconds. `Lyrics.from_item` coalesces null text to `""` and preserves
  the instrumental flag. In `beetsplug/lyrics.py`, LRCLib discards empty,
  non-instrumental results and can fall back from null plain text to synced text.
  The `find_lyrics` entrypoint and consumed result fields are unchanged. Added
  real-`Lyrics` regressions for track/album fetch and confirm with millisecond LRC,
  null text, and instrumentals; existing found/instrumental semantics are retained.
- **MusicBrainz sync keeps its existing API contract.** `beetsplug/musicbrainz.py`
  and `beets/library/fields.py` are byte-identical. Metadata-source lookup and
  direct `album_for_id` error/not-found semantics are preserved, as are
  `Distance()`, numeric distance conversion, `AlbumMatch.mapping`, and
  `AlbumMatch.apply_metadata`. The changed standalone `distance(...)` signature is
  not used by this app; upstream mbsync changes are typing/CLI-default changes.
- **Other consumed plugins remain compatible.** `fetchart.art_for_album` and
  `Candidate` keep their contracts (there is an internal legacy CAA thumbnail-name
  fallback); `lastgenre._process` keeps its contract despite a genre-stage
  refactor; `autobpm.calculate_bpm` has only an iterable annotation change.

### Import/update output: source reviewed; runtime proof is separate

`beets/logging.py` changes annotations, not `LegacyFormatter` prefixes.
`plugins.send` still logs `Sending event: {}`. `ImportTask.handle_created` still
emits `import_task_created`, and `importer/stages.py::import_asis` still logs the
bare `displayable_path(task.paths)` under the production `-A` command. The
existing progress marker and best-effort current album path therefore have
unchanged source sites.

The parser's duplicate/replacement/summary/warning literals are retained in
`importer/{tasks,stages,session}.py`: `Replacing item`, `removing … old duplicate
albums`, `deleting duplicate`, `Skipped … paths.`, `No files imported from`, and
`Resuming interrupted import`. The new `DuplicateAction.UPGRADE` and interactive
directory-rescan branches do not change the configured `REMOVE`/asis path.
`ui/commands/update.py` refactors its Python entrypoint but retains the CLI
`update -M`, mtime-skip/read-error lines, and indented `  deleted` output.
No parser rules or scan-controller command flags needed changing.

`BEETS_VERSION` and the installed-version tripwire now target 2.14.1. Fixed sample
tests, including the historical 2.13.1 session, are **not** evidence that a 2.14.1
subprocess actually emitted those lines.

### Database/path comparison and first-open window

There are **no new migrations**: `library/migrations.py`, `dbcore/pathutils.py`,
and the `Library._migrations` inventory are unchanged (nine migration classes,
eleven model-specific records). A fully migrated 2.13.1 DB has no new data
migration to run on this upgrade.

`dbcore/types.py::BasePathType.to_sql` now returns normalized `bytes` directly
instead of wrapping them in `memoryview`; SQLite still receives a BLOB.
`from_sql` still accepts legacy memoryviews and expands relative paths, while
`Item.path` remains bytes. `PathQuery` likewise binds bytes directly. No
`items.path`/`albums.artpath` schema or root-resolution change is required in
`utils.py` or the scan preflight. Added regressions check both native model path
types and Unicode BLOB normalization/snapshots, including the sibling-root guard.
The new optional query `limit` and `Results` sequence behavior do not require
changes to existing callers.

The existing first-open race is **not fixed upstream**: `Database.__init__` still
runs `_migrate`; `Migration.migrate_model` checks cached completion state before
data changes and a plain `INSERT` into the migration table's composite primary
key. `_init_beets` still constructs each `Library` outside its plugin-init lock,
which would not serialize a CLI subprocess anyway. No speculative locking fix
is included. For a fresh or incompletely migrated DB, retain the single-process,
pre-traffic migration step documented under 2.13.0. Existing fully migrated
2.13.1 databases do not gain another first-migration window or new backup files
from this release's inventory.

### Verification evidence

The independent stable-tree checker completed verification with **Python 3.14.6,
pytest 9.1.1, and beets 2.14.1** in a temporary isolated environment:

- Full backend suite: **1723 passed in 140.92s**, exit 0.
- Final `ruff check --no-cache`: **all checks passed**.
- Independent review: **approved**, with no residual findings.

Actual CLI smoke used throwaway `HOME`, `BEETSDIR`, config, statefile, database,
library, and synthetic WAV audio. It verified:

1. `beet -v import -A -C -I` emitted one album progress increment and the Unicode
   current-item path. Same-path re-import produced replacement output without
   extra DB rows; repeated incremental `-i` produced a skipped-path summary
   without false increments. Captured subprocess bytes passed through
   `parse_beets_line` and the real timestamp pump/tail consumer.
2. After deleting one temporary audio file, actual `beet -v update -M` removed
   the row and produced the parser's `removed` classification without another
   album increment. Duplicate re-import retained the configured `remove` policy
   and replaced the old temporary file in one pass.
3. SQLite paths remained relative BLOBs; native paths, exact-file/directory
   `PathQuery`, Unicode snapshots, and preflight idempotence passed. A DB created
   with actual 2.13.1 source was opened twice by 2.14.1: all eleven migration
   records and item/flex data stayed unchanged, with no new migration backups.
4. Real lyrics smoke verified null text becoming empty unsynced lyrics,
   centisecond/millisecond LRC `full_text` DB round-trips with exact SYLT
   timestamps, and instrumental boolean round-trips with the real `LyricsPlugin`
   registered. LRCLib synced-only fallback, null-result rejection, and
   instrumental acceptance passed alongside the route regressions.

No network-dependent lookup, live-library smoke, deployment, or formatting run
was performed. The shared installed environment was not modified; these results
do not claim a production deployment has occurred.

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
