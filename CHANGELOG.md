# pmtiles-torrent changelog

## master
### ✨ Features and improvements
- _...Add new stuff here..._

### 🐞 Bug fixes
- _...Add new stuff here..._

## 0.4.0
### ✨ Features and improvements
- **A torrent joined by magnet can hand back its metainfo**, which is what a client's
  "export .torrent" does. Once BEP 9 has delivered the info dictionary the node holds
  everything a `.torrent` contains, whether or not a byte of the archive has arrived — but
  nothing could ask for it, so a node that joined by magnet had no `.torrent` to publish. Its
  subscribers therefore also joined by magnet, and a magnet carrying no trackers has only the
  DHT to find a first peer with: minutes of waiting per archive rather than none. libtorrent
  could always do this; nothing had asked it to.

  Rebuilt from the parsed info rather than kept as received, since the received form is not
  retained. Verified against libtorrent that the round trip is byte-identical and preserves the
  infohash and the hybrid flags — and the caller checks the infohash anyway, so a rebuild that
  lost something cannot be published as though it were the original.

## 0.3.2
### 🐞 Bug fixes
- **Resume data is written for an archive that has only been seeding.** Saving asked
  `need_save_resume_data()` first, which answers "has anything changed since the last save"
  rather than "does a resume file exist" — and an archive that has sat there seeding since it
  was added answers no, for ever. So nothing was ever written for exactly the torrents that
  most need it, and each one re-hashed its whole store on every start: half an hour of disk
  for 800 GB before it serves anything. Saving is now unconditional, which costs a few
  kilobytes per torrent.

## 0.3.1
### 🐞 Bug fixes
- **A cache-mode torrent no longer reports 100% while holding nothing.** libtorrent's `progress`
  is a fraction of what a torrent *wants*, and cache mode wants nothing — so it answers 1.0, and
  an archive that had fetched none of its own bytes read as complete. The fraction actually held
  is reported instead, which is the same quantity the piece view already draws. The same reasoning
  was already applied to `size` a release ago; `progress` was missed.

## 0.3.0
### 🐞 Bug fixes
- **Resume data is found again, so a restart no longer re-hashes the whole store.** It was looked
  up by an infohash the caller had to supply, and nothing supplied one — but even with the lookup
  keyed off the torrent itself, `add_torrent_params.info_hashes` is only populated for params
  parsed from a magnet: with a `.torrent` it reads as forty zeros, a perfectly good name for a file
  that will never exist. The hash now comes from the metadata where there is any. Measured on a
  512 MiB archive: 1.21s and a full re-hash before, 0.02s and none after.
- **Resume data no longer discards the torrent it belongs to.** `read_resume_data` returns a fresh
  params object, and it was assigned over the one already built — losing the parsed metadata, the
  cache-mode file priorities and the paused flag, of which only the save path was put back. A
  resumed cache-mode archive would therefore have started downloading all of it.

## 0.2.0
### ✨ Features and improvements
- **Status reports the whole swarm, not only what is connected.** `peers` and `seeds` count remote
  clients this node is talking to — never itself, since a client is not its own peer — so a fully
  seeded archive nobody is currently downloading reads zero peers, which is correct and reads like
  a fault. `swarmSeeds` and `swarmPeers` carry what the tracker last reported for the swarm as a
  whole, this node included, which is what tells "nobody wants this" apart from "nobody knows about
  it". Both are -1 until a tracker has answered a scrape.
- **`pieces`**, reporting which pieces a torrent holds, how rare each one is across the swarm, and
  what each connected peer has. Reduced to a requested number of buckets before it is returned,
  because full resolution does not survive the trip: a 698 GiB archive at 4 MiB pieces is 178,000
  pieces. Availability reduces by the *rarest* piece in each bucket rather than the average — one
  piece nobody has is the answer to "can this be completed", and an average hides it. Where
  libtorrent 2.x declines to report availability (`piece_availability()` returns nothing without
  the alert-based call), it is counted from the connected peers' own bitfields instead, and
  `distributedCopies` is derived from the same data rather than left at zero beside a bar with
  data in it.
- **`rate_limits`**, setting the session's global download and upload rates on a running session.
  Applied live rather than at startup, since a setting that only took effect on restart could not
  drive a schedule.
- Document which WebTorrent major version to use. webtorrent 3 needs Node 22 but works as
  installed; webtorrent 2 runs on older Node but needs a `uint8-util` override, without which
  every magnet add throws.

### 🐞 Bug fixes
- **The libtorrent sidecar reports peers again.** `peer_info.utp_socket` is in the C++ header but
  not in the 2.x Python bindings, so reading it raised on the first peer and the caller received an
  empty list — an archive downloading at 10 MiB/s from a connected seed reported having no peers at
  all. Peer fields are now read one at a time and flags looked up by name, so an attribute a given
  build does not expose costs that attribute rather than the whole answer. Each peer also now says
  whether it is an ordinary peer, a web seed, or an HTTP seed, which the totals cannot distinguish:
  an archive pulling at full speed from one web seed and one pulling from a swarm look identical
  until that single server goes away.
- **The libtorrent sidecar no longer prints a traceback when it is stopped.** Windows delivers a
  console Ctrl-C to every process in the group, so the sidecar received it too — usually while
  blocked reading stdin — and Python printed a `KeyboardInterrupt` stack trace on the way out. The
  parent was already shutting it down deliberately; a stack trace at the end of a clean stop reads
  as a crash and buries the lines that say what actually happened. A closed pipe is treated the
  same way, for the same reason.

## 0.1.1

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
