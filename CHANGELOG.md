# pmtiles-torrent changelog

## master
### ✨ Features and improvements
- _...Add new stuff here..._

### 🐞 Bug fixes
- _...Add new stuff here..._

## 0.1.1
### ✨ Features and improvements

### 🐞 Bug fixes

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
