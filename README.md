# pmtiles-torrent

Serve a [PMTiles](https://github.com/protomaps/PMTiles) archive out of a BitTorrent swarm.

PMTiles reads an archive as a series of byte ranges. BitTorrent serves data as fixed-size,
individually verified pieces. Those two facts line up well enough that a torrent can stand in
for an HTTP origin — and unlike an HTTP origin, every client that has downloaded part of the
archive is also serving it.

This package is the mapping between the two. It implements the `Source` interface from the
`pmtiles` package, so anything that accepts a `PMTiles` instance works unchanged: tileserver-gl,
the MapLibre `pmtiles://` protocol, the CLI.

```ts
import { PMTiles } from "pmtiles";
import { TorrentSource } from "pmtiles-torrent";
import { WebTorrentEngine } from "pmtiles-torrent/webtorrent";

const engine = new WebTorrentEngine(magnetUri, { path: "/var/lib/maps" });
const archive = new PMTiles(new TorrentSource(engine));

const tile = await archive.getZxy(12, 1204, 1539);
```

## Why this works

A PMTiles archive stores tiles in Hilbert order, so tiles that are near each other on the map are
near each other in the file. Piece-granular transport means fetching a 4 KB tile costs a whole
piece — but because of that clustering, the rest of the piece is mostly tiles you are about to
want. The read amplification doubles as prefetch.

Two other properties fall out of the transport for free:

- **The infohash is a content hash.** Range reads are returned with `etag: infoHash` and
  `cache-control: immutable`. The archive cannot change under you, so PMTiles' ETag-mismatch
  retry path is structurally unreachable — a class of bug that HTTP range sources have to handle.
- **Partial seeding is the default.** Every peer serves the pieces it holds. A client that has
  only ever loaded tiles for one city is already seeding that city.

## Install

```sh
npm install pmtiles-torrent pmtiles webtorrent
```

Ships ESM, CJS and TypeScript declarations, so it works from either module system with types
either way.

`pmtiles` is a peer dependency because *you* construct the `PMTiles` instance — this package
never imports it, it only produces something `PMTiles` accepts. `webtorrent` is an *optional*
peer dependency, needed only for the WebTorrent engine; the libtorrent engine needs a Python
sidecar instead, and neither is required to install the package.

## What the source does

- **Piece mapping.** Each requested range expands to the pieces covering it, clipped to the
  archive's bounds within the torrent — so an archive packed alongside other files in a
  multi-file torrent works without special cases.
- **Parallel piece fetch.** All pieces covering one range are fetched concurrently rather than
  in sequence. A range spanning three pieces costs one swarm round-trip, not three.
- **Deduplication.** Concurrent requests touching the same piece share one fetch.
- **Reference-counted cancellation.** An aborted request stops waiting immediately, but the
  underlying piece fetch is only cancelled once *every* waiter has gone — one abandoned tile
  request cannot kill a piece another request is still blocked on.
- **Piece-counted LRU cache.** Sized as `max(64 MiB, cachePieces * pieceLength)` once metadata
  arrives, defaulting to 8 pieces. A fixed byte budget is a trap here: 64 MiB holds four 16 MiB
  pieces, few enough that one leaf-directory read evicts the tile pieces fetched moments before.
  Set `cacheBytes` for an explicit budget, or `cacheBytes: 0` to rely entirely on the engine's
  own store.
- **Over-read clamping.** PMTiles speculatively reads 16 KiB for the header regardless of archive
  size. An HTTP server truncates that for you; here it is clamped explicitly.
- **Directory prefetch.** Once the header is read, the root directory is marked critical and the
  JSON metadata high priority. Both are small and needed immediately.
- **Idle-only leaf hydration.** Leaf directories gate every tile lookup in a new region, so
  having them locally is a large win — but fetching them eagerly starves the requests they exist
  to accelerate. Measured on a 72 GiB archive against a single peer, eager prefetch took a cold
  tile from 34.2s to **138.2s**. They are therefore queued at the lowest priority, started only
  after the source has been idle, and withdrawn the instant a read arrives. Engines opt in by
  implementing `unhint()`; hydration is skipped without it, since there would be no way to call
  it off.

```js
const source = new TorrentSource(engine, {
  cachePieces: 16,          // or cacheBytes for an explicit budget
  prefetchDirectories: true,
  maxLeafPrefetchBytes: 256 * 1024 * 1024,
  hydrateIdleMs: 2000,
});

source.stats; // cacheHits, cacheMisses, bytesFetched, bytesServed,
              // cancelled, cachedPieces, cachedBytes, cacheBudget, hydrating
```

## Resume data

WebTorrent rebuilds its bitfield by hashing the entire store on every start. On a 72 GiB archive
that measured **59.9 seconds**, and it scales with size — during which the torrent has not joined
the swarm at all, which looks from the outside like "no peers".

Passing `resumePath` persists the bitfield and hands it back next time, which measured **0.6
seconds** for the same archive:

```js
new WebTorrentEngine(torrentId, {
  path: '/mnt/maps/store',
  resumePath: '/mnt/maps/store',   // same directory is fine
});
```

A bitfield asserts pieces are present without re-hashing them, so a stale one would serve
unverified bytes. It is only trusted when the data file still has the exact size and modification
time recorded when it was written, and it is discarded if the torrent it names is not the one that
loaded. Written atomically, saved on shutdown and every 60s.

With startup under a second, expect to watch the swarm connect in real time — 10 to 20 seconds for
tracker announce and handshake. That wait always existed; it used to hide behind the hashing.

## Piece size matters more than anything else here

Read amplification is `pieceLength / bytesActuallyWanted`. Torrent-creation tools pick large
pieces because they assume you want the whole file — libtorrent's automatic sizing lands at the
top of its scale for multi-hundred-gigabyte inputs. At a 16 MiB piece length, a cold 4 KB vector
tile costs a 16 MiB download.

If you control torrent creation, pick the piece length deliberately:

| Piece length | Amplification for a 4 KB tile | Pieces in a 400 GiB archive | v1 hash list |
| ------------ | ----------------------------- | --------------------------- | ------------ |
| 16 MiB       | ~4000×                        | 25,600                      | 0.5 MB       |
| 4 MiB        | ~1000×                        | 102,400                     | 2 MB         |
| 1 MiB        | ~250×                         | 409,600                     | 8 MB         |

1–4 MiB is the usual sweet spot. Below that the metadata itself (which peers must transfer via
BEP 9 before any tile can be served) starts to dominate.

BitTorrent v2 (BEP 52) improves this considerably: per-file merkle trees with 16 KiB leaf blocks
let a peer verify a small block without holding the whole hash list. See the engine notes below.

### Working with existing 16 MiB torrents

Torrents cut by `mktorrent` or libtorrent's automatic sizing commonly land at 16 MiB — a 698 GiB
planet archive comes out as 44,673 pieces. They work, and the picture is better than the raw
amplification number suggests, but the tuning changes:

- **It is a latency problem, not a waste problem.** At ~30 KB per raster tile, one 16 MiB piece
  holds roughly 500 tiles, and Hilbert ordering makes those a contiguous block on the map —
  around 24 tiles on a side, some 14 km across at z16. The first tile in a new area pays for the
  whole piece (a few seconds on a typical swarm); the next several hundred tiles nearby are free.
  What matters is that the cache is large enough to keep that piece around long enough to collect
  the payoff, which is why the default is counted in pieces.
- **Raise `cachePieces` on a server.** The default of 8 is 128 MiB per archive at this piece
  size. Somewhere between 16 and 64 is reasonable if you are serving one or two large archives
  and have the RAM.
- **Leave `maxLeafPrefetchBytes` generous.** Since hydration is idle-only it never competes with
  a request, so the 256 MiB default is safe. Do not try to force leaf directories in eagerly —
  that was measured to make cold tiles four times worse, not better.

Recutting is worth it for archives you expect to be browsed interactively, but it is not free:
re-hashing 698 GiB from local disk costs no bandwidth, but the new infohash is a new swarm, and
peers on the old one do not follow. `mktorrent -l 22` gives 4 MiB pieces. Note that `mktorrent`
emits v1-only torrents (`Info Hash v2: N/A`); for a hybrid v1+v2 torrent use libtorrent's creator
or qBittorrent's own torrent-creation dialog, which exposes the torrent format.

## Engines

The BitTorrent client is injected rather than baked in. An engine is small:

```ts
interface TorrentEngine {
  readonly key: string;                     // available synchronously, before metadata
  ready(): Promise<TorrentInfo>;            // pieceLength, fileLength, fileOffset, infoHash
  readRange(offset, length, opts): Promise<Uint8Array>;
  hint?(offset, length, priority): void;    // optional background prioritisation
  unhint?(offset, length): void;            // withdraw a hint; required for hydration
  destroy(): void | Promise<void>;
}
```

Everything PMTiles-specific lives above that line, so a new backend — or a port to another
language — only means reimplementing the engine.

`hint` and `unhint` are a pair. Idle hydration is skipped unless an engine implements both,
because queuing background work with no way to call it off is worse than not queuing it.

### WebTorrentEngine (bundled)

Accepts a magnet URI, a bare infohash, or a path to a `.torrent` file.

```ts
new WebTorrentEngine(torrentId, {
  client,         // reuse one client across archives: one peer pool, one port, one DHT node
  path,           // chunk store location; persist it to keep seeding across restarts
  resumePath,     // where to keep resume data — see above; usually the same as `path`
  filePath,       // pick a file inside a multi-file torrent
  announce,       // extra trackers
  maxWebConns,    // connections per web seed; WebTorrent's own default of 4 is low
  readyTimeoutMs, // default 60s
});
```

The torrent is added with `deselect: true`, so nothing downloads until a range is requested.
Without that, WebTorrent selects every piece and starts pulling the entire archive.

**Prefer a `.torrent` file over a magnet.** A magnet carries only an infohash, so the client must
find peers and complete a BEP 9 metadata exchange before it knows anything about the archive —
measured at somewhere between 90 and 240 seconds against a 72 GiB archive. A `.torrent` already
contains the metadata and was ready immediately.

WebTorrent is the only engine that can bridge both halves of a swarm: browser peers speak WebRTC
and conventional clients speak TCP/uTP, and they cannot see each other directly. A
WebTorrent-based server is what lets one swarm serve both.

**This is a BitTorrent v1 engine.** WebTorrent does not implement BEP 52 — no merkle
verification, no `btmh` magnets. For those, use the libtorrent engine below.

### LibtorrentEngine

Talks to libtorrent through a Python sidecar shipped in this package. It adds three things
WebTorrent cannot:

- **`set_piece_deadline`** — promotes one piece to the front of the queue rather than waiting for
  the normal picker, which is exactly what a blocking tile read wants.
- **Per-piece priorities** — hydration can sit at priority 1 and drop to 0 the instant a read
  arrives, rather than selecting and deselecting ranges wholesale.
- **BitTorrent v2 (BEP 52)** — per-file merkle trees with 16 KiB leaf blocks, so a peer can verify
  a small block without holding the whole hash list. The right shape for random access.

```ts
import { LibtorrentEngine } from "pmtiles-torrent/libtorrent";

new LibtorrentEngine(torrentId, {
  path,            // data store — required
  resumeDir,       // libtorrent's own resume data; skips re-hashing on start
  python,          // executable, default "python3"
  listen,          // e.g. "0.0.0.0:6881"
  maxConnections,  // peer cap; every peer is a NAT table entry
});
```

Needs libtorrent's Python bindings: `apt install python3-libtorrent` on Debian/Ubuntu,
`brew install libtorrent-rasterbar` on macOS, or `pip install libtorrent` where wheels exist.

WebTorrent stays the default: it needs no external install and is the only option in a browser.
Reach for libtorrent on a server that wants v2, faster piece prioritisation, or scale.

## Web seeds

If the torrent carries a BEP 19 `url-list` — an HTTP URL serving the same bytes — it is used
automatically, and it changes the picture more than any client tuning does. A web seed is always
available and usually far faster than a small swarm, so it removes both the wait for a first peer
and the bandwidth ceiling of a handful of seeders.

Measured with DHT and trackers **disabled entirely**, a tile was served in 673 ms with no
BitTorrent peers at all — every byte over HTTP.

Nothing needs configuring on this side; the URL is in the torrent. What matters is that whoever
creates the torrent includes it (`mktorrent -w https://…`). The one setting worth raising is
`maxWebConns`, since WebTorrent allows only four simultaneous connections per web seed by default
— which throttles exactly the source most worth leaning on.

If you publish archives over HTTP already, adding a web seed to their torrents is the single
largest improvement available here.

## Development

TypeScript source in `src/`, compiled by tsup to ESM, CJS and declarations in `dist/`. Consumers
install the compiled output, so nothing downstream needs a toolchain.

```sh
npm install
npm run tsc     # typecheck
npm test        # 36 tests
npm run build   # dist/esm, dist/cjs, .d.ts
```

`prepublishOnly` runs all three, so a package that does not typecheck, pass or build cannot be
published.

Tests run against an in-memory engine with controllable timing, so concurrency and cancellation
behaviour is covered without a swarm, plus an end-to-end read of a real PMTiles archive fixture.
The libtorrent sidecar is exercised separately in CI, which installs libtorrent to compile and
smoke-test it — otherwise it is the one component nothing checks.

### Releasing

Two stages, so nothing publishes by accident:

1. **Actions → Create bump version PR** — bumps the version, promotes the `## master` changelog
   section to that number, and opens a PR. Nothing is published here.
2. **Create a GitHub Release** tagged `vX.Y.Z` once that PR is merged. That triggers the publish,
   which refuses to run if the tag does not match `package.json`, and derives the dist-tag from
   the version so a prerelease cannot become `latest`.

Publishing uses npm trusted publishing (OIDC) from the `release` environment — there is no
long-lived npm token.

## License and attribution

BSD-3-Clause — see [LICENSE](LICENSE).

Third-party work this project derives from or redistributes is recorded in [NOTICE.md](NOTICE.md):
PMTiles (BSD-3-Clause / CC0-1.0) for the v3 header layout and the test fixture, and WebTorrent
(MIT) for the API the bundled engine targets. No qBittorrent code is used — it is GPL-2.0+ and
incompatible with redistribution here.
