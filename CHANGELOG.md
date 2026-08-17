# pmtiles-torrent changelog

## master

### ✨ Features and improvements

- _...Add new stuff here..._

### 🐞 Bug fixes

- _...Add new stuff here..._

## 0.5.0

### ✨ Features and improvements

- **The sidecar can say whether peers are able to reach this node.** `reachability` reports one of
  three states from libtorrent's own figures: `open` when something has connected inward,
  `unproven` when the session is listening and nothing ever has, and `offline` when it is not
  listening at all.

  It reads `net.has_incoming_connections`, which is the honest signal and latches for the life of
  the session — "has anything ever connected inward" rather than "is one open now" — so a node
  that was reachable an hour ago and is merely quiet stays green instead of flickering every time
  the last peer leaves. `is_listening()` and `listen_port()` answer the third state directly, which
  is why none of this needs the listen alerts the read loop already drains.

  The middle state is deliberately not called firewalled. On a node no peer has tried, blocked and
  untried are the same observation, and nothing available can separate them — so it is reported as
  unproven, with the peer counts beside it, rather than as a fault somebody would go looking for.

### 🐞 Bug fixes

## 0.4.6

### ✨ Features and improvements

- **The end of an archive is fetched before anything has read its header.** Everything the source
  prioritises -- the root directory, the JSON metadata, the leaf directories -- is derived from the
  header, which leaves a gap at the start: until a header can be read, nothing points anywhere but
  at the header, so the structural sections are only ever asked for on a second pass. planetiler
  uses the spec's permission to relocate sections and writes the JSON metadata and then the leaf
  directories _after_ all the tile data, so on those archives the tail is structure rather than
  tiles -- and it is the half a partial mirror is least likely to hold, since tile data arrives in
  whatever order the swarm offers while nothing at all asks for the end. One piece of it is now
  hinted at normal priority as soon as the geometry is known, below the header everything is
  blocked on and above the tile data nobody asked for. Deliberately a single piece: on a
  canonically laid out archive, tippecanoe's among them, the tail is ordinary tile data and this
  fetches a piece nobody wanted -- a fair price for removing a round trip from every planetiler
  archive, and not one worth paying at a larger window.

### 🐞 Bug fixes

- **The sidecar was told why a read failed and threw it away.** The loop that waits for a piece
  drains the session's alert queue and kept only the `read_piece_alert`, discarding everything else
  -- including `torrent_error_alert` and `file_error_alert`, which are how libtorrent says a piece
  could not be written or a file could not be opened. The session subscribes to
  `error_notification` and `storage_notification` precisely so those arrive, and then the one loop
  running while a read is outstanding binned them. A full disk, an unwritable save path and a
  torrent that cannot verify its pieces were all reduced to the same silent timeout -- and because
  draining removes them from the queue, nothing else could see them either. They are now
  collected, appended to the error the caller gets, and said once on stderr, since a storage
  failure outlives the request that noticed it and the next caller has no other way to learn it
  happened.

## 0.4.5

### 🐞 Bug fixes

- **A torrent that was not ready yet reported a corrupt one.** Reading a piece from an archive whose
  metadata had not arrived — or that was still checking what is on disk, which is how a resync
  starts — was answered by libtorrent as `invalid piece index in slot list`. The piece count is zero
  until metadata lands, so every index is out of range, including the valid ones. What is really
  "ask again in a moment" therefore arrived under a name that reads as a damaged torrent, and a
  caller retrying on a backoff paid minutes for a condition that clears in seconds.

  `op_read_piece` now waits for the torrent to become readable inside the caller's existing timeout,
  which is the honest shape for it: the budget is already there and the condition resolves on its
  own. If the wait runs out it says `metadata has not arrived yet` — the same wording `op_info`
  already uses for the same condition, so the two entry points finally agree — or names the state it
  is stuck in. An index genuinely out of range now says so and says the piece count, and any error
  libtorrent does raise carries the torrent's state, pieces held and peer count with it.

  Worth knowing how much rests on this one read: the PMTiles v3 spec requires the root directory to
  lie within the first 16,384 bytes, so a 16 KiB read at offset 0 fetches the header and the root
  directory together — a single piece, after which the archive is servable. A node mirroring an
  archive is unservable until exactly this read succeeds.

- **The sidecar's own unit tests were never run.** `test:sidecar` named one file, and CI never
  invoked it at all — so the one component CI installs libtorrent for was the one component it could
  not catch a regression in. The script now discovers every `test_sidecar*.py`, and CI runs it.

## 0.4.4

### 🐞 Bug fixes

- **The piece bars only told the truth at 0% and 100%.** A column covers many pieces — 178,000
  across a bar a thousand wide on a 698 GiB archive — and "held" reduced by `all`, so a column lit
  only when every piece beneath it had arrived. On an archive 18% downloaded, scattered, no column
  qualified and the bar read as completely empty while the torrent was plainly working.

  Peer bars had the opposite fault, reducing by `any`: one piece in a hundred and seventy-eight lit
  the whole column, so a peer holding almost nothing looked like a seed.

  Both now report a **proportion** (0–255), which is the only honest reduction when a column stands
  for many pieces. A non-empty bucket never rounds below 2, so a reader can tell this encoding from
  the old booleans by whether any value exceeds 1 — which is how pmtiles-swarm keeps rendering
  correctly against either.

## 0.4.3

### ✨ Features and improvements

- **[examples/maplibre-gl-js](examples/maplibre-gl-js)** — a browser page that reads tiles out of
  an archive it is downloading from the swarm, with no tile server involved. The integration point
  is `addProtocol`: it claims a URL scheme, which is what lets an ordinary-looking style entry route
  into this package, and it is the same mechanism `pmtiles://` uses.

  Two things it demonstrates that are easy to miss. `TorrentSource` already implements the pmtiles
  `Source` interface — `getBytes()` and `getKey()` — so the `pmtiles` library reads through a
  swarm without knowing anything about BitTorrent, and nothing in the path is tile-aware. And the
  TileJSON is **derived from the archive** rather than fetched: the header carries the zoom range,
  bounds and centre, the metadata carries the name and vector layers, so no server is needed to
  describe a map, only to host bytes.

  **This package already runs in a browser unmodified.** It has no runtime dependencies, and every
  Node-only path in the WebTorrent engine is gated behind `resumePath` — leave it unset and
  `node:fs`, `node:path` and `Buffer` are never reached.

  The example's README is explicit about what a browser cannot do, since those are platform limits
  rather than gaps here: WebRTC only, so a magnet needs a `wss://` tracker or a page will find no
  peers however healthy the swarm is elsewhere; a cold tile costs a whole piece, so at 4 MiB
  pieces one 40 KB tile means moving 4 MiB; and nothing persists between visits. The honest use is
  bandwidth rather than latency — every piece a viewer pulls is one they seed back, so a region
  many people view at once gets cheaper to distribute as the audience grows.

## 0.4.2

### 🐞 Bug fixes

- **A hybrid torrent is now named by its v1 infohash, which is the name everything else knows it
  by.** For a hybrid v1+v2 torrent libtorrent answers `info_hash()` with the _truncated v2_ hash,
  and the sidecar reported that as the torrent's identity — while the catalog that recorded it,
  the magnet handed to peers and every v1 client in the swarm use the v1 hash. A freshly built
  archive therefore seeded perfectly well while the node holding it reported an archive its engine
  had never heard of, served no tile from it, because every lookup arrived under a name the
  sidecar had not filed it under, and wrote its resume file under that other name too, so every
  restart re-hashed it.

  Only newly created archives were affected, since creation is where hybrids come from: one
  archive out of seventeen on the node this was found on. It reads as a single corrupt build
  rather than as a naming fault, which is what made it hard to see.

  All six places that minted the identity — create, add, info, status, resume writing and the
  handle lookup — now go through one helper that prefers v1 and falls back to the truncated form
  for a v2-only torrent, where it is the only name there is. Existing hybrid archives will
  re-check once as their resume files are rediscovered under the corrected name.

## 0.4.1

### 🐞 Bug fixes

- **An archive whose data is already on disk no longer waits to be hashed a second time.** The
  sidecar's add now understands `seedOnly`: the caller's statement that the store is complete and
  was verified on the way in, which for an archive created from a local file it was — the file
  had just been read end to end to produce the torrent. Without it libtorrent re-hashed the whole
  archive before it would seed a byte. For an 81 GiB planet build that is roughly a quarter of an
  hour of disk to rediscover what had been measured moments earlier, and for the whole of it the
  archive reads as 0% and serves nobody, which looks like a failed import rather than like work in
  progress.

  This is libtorrent's `seed_mode`, and it stays a claim rather than an assumption: it is set only
  when the caller makes it, never for cache mode, which by definition holds no complete copy. If
  the claim turns out to be wrong libtorrent verifies the piece before sending it and falls back
  to checking, so the cost of being wrong is a re-check rather than bad data on the wire.

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
  is a fraction of what a torrent _wants_, and cache mode wants nothing — so it answers 1.0, and
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
  pieces. Availability reduces by the _rarest_ piece in each bucket rather than the average — one
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
- Idle-only leaf-directory hydration. Eager prefetch was measured making a cold tile _worse_ — 34.2s to 138.2s — by starving the requests it was meant to accelerate.
- Two engines: WebTorrent (default, works in a browser) and libtorrent via a Python sidecar, which adds `set_piece_deadline`, per-piece priorities and BitTorrent v2.
- Recognises magnet URIs, bare infohashes and `.torrent` files; a `.torrent` skips the BEP 9 metadata exchange and reaches peers far sooner.

### 🐞 Bug fixes

- Web seeds are used when a torrent carries a BEP 19 `url-list`, which removes both the cold-start wait and the bandwidth ceiling of a small swarm — a tile was served in 673 ms with DHT and trackers disabled entirely.
