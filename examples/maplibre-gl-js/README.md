# Reading an archive over BitTorrent, in a browser

A maplibre-gl-js page that resolves tiles out of a PMTiles archive it is
downloading from a swarm, with no tile server involved. The style entry is an
ordinary `pmtiles://` source — the same one a page without this library would
use — and the swarm is wired in underneath it.

```sh
npm install && npm run build     # from the repository root
npx serve examples/maplibre-gl-js # or: python -m http.server
```

Then edit `ARCHIVE_URL` and `MAGNET` at the top of `index.html` to point at a
real archive — copy both from a pmtiles-swarm console, or out of the `torrent`
block of any TileJSON it serves.

Load the page with `?http` to skip the swarm. Same style, same source URL, no
code changes — that comparison is the thing this example exists to show.

## Versions

The import map pins **maplibre-gl 6**, **pmtiles 4** and **webtorrent 3** — the same
majors pmtiles-swarm runs, so the browser side of a swarm is exercised against
the versions its nodes actually use. This package's `peerDependencies` are
looser (`webtorrent >=2`, `pmtiles >=3`) and older majors do work; matching what
is deployed is the point of an example.

## What it demonstrates

**`TorrentSource` is a drop-in, not a replacement.** It implements the pmtiles
`Source` interface — `getBytes()` and `getKey()` — so it substitutes for the
`FetchSource` the library would otherwise build for itself. Nothing in this path
is tile-aware: the archive is one file and PMTiles resolves a tile to a byte
range, exactly as it would over HTTP.

**One URL, two transports.** `Protocol.add()` keys an instance by
`source.getKey()`, and the protocol handler looks up whatever follows
`pmtiles://` *before* constructing anything of its own. Register a
`TorrentSource` under that exact string and it wins; skip the registration and
the identical URL loads over HTTP. So a style can be written once and read by
clients that know nothing about any of this.

**The magnet travels in the fragment.**

```
pmtiles://https://swarm.example.org/files/example.pmtiles#magnet:?xt=urn:btih:…&ws=…
```

A fragment is never sent in an HTTP request, so this is an ordinary archive URL
to anything that does not look at it, and a complete swarm address to anything
that does. It also survives the protocol's own URL handling, which appends
`/{z}/{x}/{y}` and re-parses with a greedy pattern.

**The key must match character-for-character.** `key: SOURCE_URL` in the example
is load-bearing. A key that does not match does not raise an error — the lookup
misses, a `FetchSource` is built, and the map loads over HTTP looking exactly as
it should. Check the panel, not the map, when the swarm appears to do nothing.

**No build step for this package.** Every Node-only path in the WebTorrent
engine is gated behind `resumePath`; leave it unset and `node:fs`, `node:path`
and `Buffer` are never reached. The package has no runtime dependencies, so
nothing else drags Node in either.

## What a browser cannot do

Worth understanding before judging the numbers, because these are properties of
the platform rather than of this code.

**WebRTC only.** A browser cannot reach TCP or uTP peers *at all*. Its swarm is
whatever speaks WebRTC: other browsers, and nodes running a WebTorrent engine —
which is why pmtiles-swarm can run libtorrent and WebTorrent side by side. A
magnet whose only trackers are `udp://` will find nobody from a page, however
healthy that swarm is elsewhere. Use a `wss://` tracker.

**A cold tile costs a whole piece.** At 4 MiB pieces, one 40 KB tile that is not
cached means moving 4 MiB over WebRTC. HTTP would have delivered it in
milliseconds. So in a browser the swarm belongs in the background and prefetch
path, and never in front of a first paint.

**Nothing is stored between visits.** Resume data is deliberately off, so each
load starts cold. `cachePieces` bounds what is held in memory.

**The client is not small.** WebTorrent's browser build is several hundred KB.
Worth loading lazily, after the map is already usable — which the drop-in shape
makes straightforward, since the map works before the registration happens.

## So what is it for

Not latency — for a single visitor, HTTP wins every time.

It is bandwidth. Every piece a viewer pulls is a piece they serve back, so a
region that many people are looking at *at the same time* gets cheaper to
distribute as the audience grows, which is the opposite of how a tile server
behaves under load. The panel in the corner shows both numbers, because the one
that matters here is the second: bytes seeded back, not bytes received.

The honest arrangement in production is both at once — HTTP for the first paint
and for anything the swarm cannot answer quickly, the swarm for everything
after. The same convention works one level up, on a TileJSON URL, when you would
rather a server described the map:

```
https://swarm.example.org/latest/openmaptiles/tiles.json#magnet:?xt=urn:btih:…&ws=…
```
