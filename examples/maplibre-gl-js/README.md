# Reading an archive over BitTorrent, in a browser

A maplibre-gl-js page that resolves tiles out of a PMTiles archive it is
downloading from a swarm, with no tile server involved. The map's source is an
ordinary style entry; the only unusual part is the URL scheme.

```sh
npm install && npm run build     # from the repository root
npx serve examples/maplibre-gl-js # or: python -m http.server
```

Then edit `MAGNET` at the top of `index.html` to point at a real archive — copy
one from a pmtiles-swarm console, or out of the `torrent` block of any TileJSON
it serves.

## Versions

The import map pins **maplibre-gl 6**, **pmtiles 4** and **webtorrent 3** — the same
majors pmtiles-swarm runs, so the browser side of a swarm is exercised against
the versions its nodes actually use. This package's `peerDependencies` are
looser (`webtorrent >=2`, `pmtiles >=3`) and older majors do work; matching what
is deployed is the point of an example.

## What it demonstrates

**`addProtocol`.** It claims a URL scheme, so a normal-looking style entry
routes to your code. It is the same mechanism `pmtiles://` uses.

**The TileJSON is derived, not fetched.** Everything a TileJSON carries is
already in the archive: the header has the zoom range, bounds and centre, and
the metadata has the name and vector layers. So no server describes the map —
one only hosts the bytes, and even that becomes optional once there are peers.

**`TorrentSource` is a pmtiles `Source`.** It implements `getBytes()` and
`getKey()`, so the `pmtiles` library reads through the swarm without knowing
anything about BitTorrent. Nothing in this path is tile-aware: the archive is
one file and PMTiles resolves a tile to a byte range, exactly as it would over
HTTP.

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
Worth loading lazily, after the map is already usable.

## So what is it for

Not latency — for a single visitor, HTTP wins every time.

It is bandwidth. Every piece a viewer pulls is a piece they serve back, so a
region that many people are looking at *at the same time* gets cheaper to
distribute as the audience grows, which is the opposite of how a tile server
behaves under load. The panel in the corner shows both numbers, because the one
that matters here is the second: bytes seeded back, not bytes received.

The honest arrangement in production is both at once — HTTP for the first paint
and for anything the swarm cannot answer quickly, the swarm for everything after
that. A pmtiles-swarm TileJSON URL with the magnet in its fragment carries what
you need for both in a single string:

```
https://swarm.example.org/latest/openmaptiles/tiles.json#magnet:?xt=urn:btih:…&ws=…
```

A fragment is never sent in an HTTP request, so that URL still works unchanged
in a page that has none of this wired up.
