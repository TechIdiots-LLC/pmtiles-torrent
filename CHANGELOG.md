# pmtiles-torrent changelog

## master
### ✨ Features and improvements
- _...Add new stuff here..._

### 🐞 Bug fixes
- _...Add new stuff here..._

## 0.2.0

Documentation only. `src/` and `package.json` are byte-identical to 0.1.0 — this
release exists because the page npm shows described the package as it was before
it was extracted into its own repository.

### ✨ Features and improvements
- **README rewritten for the standalone package.** The Development section still said this
  lived inside tileserver-gl, was consumed as a `file:packages/pmtiles-torrent` dependency,
  was plain ESM with JSDoc types, and had no build step. All four were wrong. It now covers
  the TypeScript build, the `prepublishOnly` gate that blocks publishing anything which does
  not typecheck, pass and build, and the two-stage release.
- **The libtorrent engine is documented**, having had no section at all despite being what
  provides BitTorrent v2, per-piece priorities and `set_piece_deadline`. Includes what to
  install and when to prefer it over WebTorrent.
- **Web seeds are documented**, previously unmentioned despite being the largest performance
  lever available here: a torrent carrying a BEP 19 `url-list` served a tile in 673 ms with
  DHT and trackers disabled entirely. Notes that `maxWebConns` is worth raising, because
  WebTorrent's default of four connections per web seed throttles exactly the source most
  worth leaning on.
- **`.torrent` files are documented as preferable to magnets.** A magnet must complete a
  BEP 9 metadata exchange before anything else can happen — measured between 90 and 240
  seconds against a 72 GiB archive, where a `.torrent` was ready immediately.
- `unhint` added to the documented engine interface. It is half of a pair: idle hydration is
  skipped unless an engine implements both, since queuing background work with no way to
  call it off is worse than not queuing it.
- `WebTorrentEngine` options now list `resumePath` and `maxWebConns`, and the constructor is
  described as taking a magnet, a bare infohash **or** a `.torrent` path.

### 🐞 Bug fixes
- The install note claimed `pmtiles` was a peer dependency "used only for types at build
  time". This package never imports `pmtiles` at all — the consumer constructs the `PMTiles`
  instance, and this only produces something that instance accepts.

## 0.1.0
### ✨ Features and improvements
- TypeScript source, compiled to a JavaScript package. Consumers get ESM, CJS and real `.d.ts` declarations from `dist/`; nothing needs a toolchain to install it.
- Initial release: a torrent-backed `Source` for the `pmtiles` package, so a PMTiles archive can be read straight out of a BitTorrent swarm without downloading it first.
- Piece mapping with parallel fetch, request deduplication, and reference-counted cancellation — one abandoned tile request cannot starve another waiting on the same piece.
- Piece-counted LRU cache, sized `max(64 MiB, cachePieces × pieceLength)`. Counted in pieces because a fixed byte budget holds too few of the 16 MiB pieces large archives are cut with.
- Resume data: persisting WebTorrent's bitfield took startup on a 72 GiB archive from 59.9s to 0.6s. Only trusted when the data file's size and mtime still match.
- Idle-only leaf-directory hydration. Eager prefetch was measured making a cold tile *worse* — 34.2s to 138.2s — by starving the requests it was meant to accelerate.
- Two engines: WebTorrent (default, works in a browser) and libtorrent via a Python sidecar, which adds `set_piece_deadline`, per-piece priorities and BitTorrent v2.
- Recognises magnet URIs, bare infohashes and `.torrent` files; a `.torrent` skips the BEP 9 metadata exchange and reaches peers far sooner.

### 🐞 Bug fixes
- Web seeds are used when a torrent carries a BEP 19 `url-list`, which removes both the cold-start wait and the bandwidth ceiling of a small swarm — a tile was served in 673 ms with DHT and trackers disabled entirely.
