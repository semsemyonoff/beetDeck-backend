# beets upgrade notes

Per-release findings from bumping the beets pin in `pyproject.toml`. Newest first.
Add an entry when you bump the version; the current pin itself lives in
`pyproject.toml`, not here.

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
