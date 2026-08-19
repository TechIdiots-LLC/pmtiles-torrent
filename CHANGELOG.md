# pmtiles-torrent changelog

## master
### ✨ Features and improvements
- _...Add new stuff here..._

### 🐞 Bug fixes
- _...Add new stuff here..._

## 0.10.1
### 🐞 Bug fixes
- **0.10.0's `skipVerify` broke every read from a browser.** It reads like "do not waste time
  checking a store that is empty anyway" and means the opposite: WebTorrent's own `seed()` sets it to
  declare the data already complete. On a store holding nothing the torrent claimed all 178,690
  pieces, never downloaded one, and the first read failed inside the store with `Index 0 does not
  exist`. Removed, with the reasoning written down where the next person will look for it.

  The verify pass is what leaves the bitfield honest, and an honest bitfield is what makes the first
  read fetch a piece rather than look for one. The scaled `readyTimeoutMs` from 0.10.0 is what makes
  a large torrent joinable, and stands.

## 0.10.0
### ✨ Features and improvements
- **A large torrent can be joined from a browser.** Bringing one up costs time before it costs
  bandwidth: WebTorrent walks every piece before `ready` fires, so a fixed metadata budget quietly
  excluded the archives most worth sharing. Measured against a live swarm, a 749 GiB build is 178,690
  pieces where an 80 GiB one is 20,636 — the same 30-second budget joined the second and timed out on
  the first, which then fell back to plain HTTP.

  - **No verify pass without a store.** `path` unset means nothing is persisted, so a verify can find
    nothing — and it is not free, being the whole budget spent proving an empty store is empty.
    `skipVerify` is now set in that case. A node that keeps a store verifies as before, since there
    it has something worth checking.
  - **The budget grows with the torrent.** `readyTimeoutMs` gains a millisecond per piece where the
    metainfo was supplied outright and the count can be read from it. A magnet carries no piece
    count, so it keeps the base budget.

  Neither refuses to join a large archive on the grounds of size. A big archive is exactly the one
  where seeding is worth having.

### 🐞 Bug fixes

## 0.9.1
### ✨ Features and improvements

### 🐞 Bug fixes
- **Resume data that has not changed is no longer rewritten.** A hybrid torrent carries a merkle tree
  of 32 bytes per 16 KiB block in its resume data — a few hundred megabytes for a 128 GiB archive,
  several gigabytes for a planet build — and the periodic save asked every torrent unconditionally,
  every five minutes, staging and fsyncing and renaming the lot to record that nothing had moved.

  Asking unconditionally was itself a fix: `need_save_resume_data()` reports change since the last
  save, not whether a file exists, so a torrent that had sat seeding since it was added answered
  "nothing has changed" and nothing was ever written for it — and it re-hashed its whole store on
  every start. Both properties hold if the file's existence decides rather than the flag alone. The
  first save always happens; the rewrites stop.
- **`save_resume` now gives each torrent its own share of the budget.** Five seconds was a fixed
  total for the whole library: whichever alerts arrived inside it were written and the rest were
  silently dropped, so a node with four archives persisted two of them, and a different two next
  time. Each one that missed re-hashed its entire store on the following start. It is two seconds per
  torrent now, and the reply carries `asked` alongside `written` so a caller can see the shortfall
  rather than infer it.

## 0.9.0
### ✨ Features and improvements
- **`pause` and `resume`, which did not exist.** A consumer had no way to stop a torrent short of
  removing it, and removing it drops the resume data — so resuming a 698 GiB archive meant hashing
  the whole store again to arrive back where it started. Reported from the field as a pause that
  did nothing: the row read `paused` while the archive went on downloading at 8.4 MiB/s.

### 🐞 Bug fixes
- **A paused torrent no longer starts itself again a second later.** `handle.pause()` on its own is
  not a stop. libtorrent's auto-manager owns the paused flag of every torrent carrying
  `auto_managed`, and clears it again within about a second — measured against 2.0.13: paused at
  0.2s, running at 1.0s, still running at six. Since the flag is what a status reports, that is an
  archive describing itself as paused while it transfers. `auto_managed` is now taken away when a
  torrent is stopped and given back when it starts, except in cache mode, which is kept out of the
  auto-manager on purpose — it wants no bytes, so the manager reads it as idle and pauses it, and a
  paused torrent stops seeding.
- **Adding an archive paused now keeps it paused.** The same defect on the path a restart takes:
  the `paused` flag was set on the add and the auto-manager cleared it, so every archive that had
  been paused came back up transferring.

## 0.8.0
### ✨ Features and improvements
- **`--create` hashes one archive in a process of its own, so a hash can be cancelled and can say
  how far it has got.** libtorrent's hashing never checks for interruption, so the only way to stop
  one is to end the process running it — and the sidecar cannot be ended, because it holds the
  session and every torrent seeding from it. A 698 GiB build started by a misclick therefore ran
  its full six hours, saturating the disk the rest of the library was being served from.

  The one-shot holds no session and opens no port, so killing it costs the hash and nothing else;
  hashing only ever reads, so the archive is untouched. It speaks the same line-delimited JSON as
  the pipe protocol, reading its parameters from stdin:

      <- {"event": "progress", "piece": 4096, "pieces": 178234}
      <- {"ok": true, "result": {...}}

  Progress is throttled by time rather than piece count — 178,000 pieces would otherwise be
  178,000 lines — and the last piece always reports, so a caller drawing a bar reaches 100%. The
  callback carrying it is the one that already had to exist to release the GIL.

  `create` over the pipe is unchanged and still works. Both now share one `build_torrent`.

### 🐞 Bug fixes

## 0.7.5
### ✨ Features and improvements

### 🐞 Bug fixes
- **An archive hashing its store said "paused".** libtorrent hashes one store at a time —
  `active_checking` defaults to 1 — and every torrent in that queue carries the paused flag,
  including the one being hashed. The paused flag was tested before the state, which made
  "checking" all but unreachable: on the restart that recovered eighteen archives the whole library
  read as paused, with no way to tell work in progress from an archive somebody had stopped.

  Hashing now outranks the flag. Only `checking_files` does, though, not `checking_resume_data`: a
  torrent genuinely held back rests in that second state, and calling it work in progress would be
  the same confusion reversed.

## 0.7.4
### ✨ Features and improvements

### 🐞 Bug fixes
- **Saving resume data was corrupting it, and archives disappeared from the session a few more
  with every restart.** Reported from the field as `[restore] <archive>: mismatching info-hash`,
  starting with one archive and reaching eighteen over successive restarts. An archive that failed
  this way was never added at all: it showed 0% and no state in the console, a recheck answered
  `no such torrent`, and its data sat complete on the disk the whole time — a preview rendered
  from it perfectly well.

  `op_save_resume` was the last consumer still popping the session's alert queue itself, on its own
  thread, while the pump popped on another. Two threads on one destructive queue is not a race that
  can be narrowed: the pump's next `pop_alerts()` frees the batch the other is still reading, so
  `alert.handle` and `alert.params` became reads of memory the session had reclaimed. What reached
  the disk was resume data under another torrent's name.

  libtorrent parses such a file without complaint — it is `add_torrent` that rejects it, because
  real resume data carries its own infohash. Confirmed against 2.0.13: save a torrent's resume
  data, file it under a different torrent's name, and the add fails with exactly
  `mismatching info-hash [libtorrent:30]`.

  This is the same fault 0.7.1 fixed for piece reads, in the one place that was missed. Resume data
  is now collected through the pump like everything else, with the infohash and the serialised
  buffer both taken on the pump's thread while the alert is valid. 0.7.2 did not introduce this,
  but it made the pump pop far more often, which widened the window enough to make it routine.

- **A resume file the torrent cannot use now costs a recheck rather than the archive.** Whatever
  the reason — a file spoiled before this release, a truncated write, anything — the add is retried
  without it, the recheck finds every byte that is on disk, and nothing is downloaded again. The
  unusable file is discarded rather than left to fail the same add on every future start.

- **Resume data is written atomically.** Staged beside the target and renamed over it, with an
  fsync so the rename cannot reach the disk before the bytes. An interrupted write leaves the
  previous good copy instead of a truncated file under the real name.

## 0.7.3
### ✨ Features and improvements

### 🐞 Bug fixes
- **A resume file that cannot be parsed costs a recheck, not the archive.** It used to raise
  straight out of `add`, so restoring the library skipped that archive entirely: never in the
  session, absent from every listing, and nothing to recheck. (The archives disappearing in the
  field were doing so for a different reason — see 0.7.4 — but the brittleness was real.)

- **A torrent removed and immediately added back could vanish from listings for good.**
  `torrent_removed_alert` arrives well after the removal that caused it, and 0.7.2 dropped the
  handle on that alert — landing on a registration made after it. Removing and re-adding is
  ordinary: it is how a library is restored and how a mode change is applied.

  What made it permanent rather than a flicker: `post_torrent_updates()` reports only torrents that
  have *changed*, so a complete archive sitting there seeding is described once and never again.
  The alert now drops only the cached status, and only when the torrent really has gone; `remove`
  drops the handle itself, where there is nothing to race.

- **A listing can no longer report a smaller library than the sidecar is holding.** The safety net
  under the above, and the reason that bug could bite at all. Anything in the session with no
  cached status is now asked to describe itself — asynchronously, so no round-trip — instead of
  being silently omitted until something about it happens to change.

- **One bad alert no longer kills the alert pump.** Absorbing an alert ran outside the pump loop's
  own guard, so an exception ended the thread — taking piece reads, status updates and fault
  reporting with it, silently.

## 0.7.2
### ✨ Features and improvements

### 🐞 Bug fixes
- **`libtorrent list timed out after 60000ms`, on a node that was otherwise working.** Seeding,
  downloading and tile reads all carried on; only the console could not be told about them, so its
  header sat at "connecting…" and clicking an archive never loaded its details. 0.7.0 and 0.7.1
  both aimed at this and neither reached it, because the cost was not in the request loop.

  It was in how a listing was assembled. Reading one torrent's state costs a blocking round-trip to
  libtorrent's session thread, and the listing did three per torrent — `status()`, `flags()` and
  `torrent_file()` — so twenty archives cost sixty, each queued behind whatever that one thread was
  doing. Measured on a real session holding twenty torrents: 0.66ms idle, and **1001ms** with the
  session thread busy hashing. Same torrents, same work, 1500× the wait — and a slow disk under a
  698 GiB hash is far past that. The disk was never the whole story; a multiplier of sixty was.

  So nothing asks any more. `post_torrent_updates()` is asynchronous — it queues a message and
  returns — and the session answers with one alert describing every torrent that changed. The alert
  pump keeps a dictionary of rendered states current, and `list` reads that dictionary. Every field
  came off the status object all along, including the two that were being fetched from the handle
  separately. Listing now cannot block on the session at all, which is the point: the console keeps
  working while the session thread is busy. Figures are at most a second old.

  Held to by a test that swaps the session for one that raises on any call and asserts the listing
  still answers. `get_torrent_status()`, the other batched route, is not usable from these bindings
  — it invokes the Python predicate on libtorrent's thread without the GIL, and segfaults.

## 0.7.1
### ✨ Features and improvements

### 🐞 Bug fixes
- **The alert pump added in 0.7.0 could crash the sidecar with SIGSEGV.** libtorrent owns its
  alerts and frees them on the next `pop_alerts()`, so an alert object handed to another thread is
  a pointer into memory the session is about to reuse. The pump queued the alert itself, and a
  reader waits up to 500ms before looking at its queue while the pump pops about twice a second —
  so the read was routinely of freed memory. That is not an exception, it is the process dying.
  Confirmed against libtorrent 2.0.13: capture alerts, pop five more times, read one of the
  captured ones, and the interpreter dies with a memory-corruption code.

  A subscription now takes a snapshot on the pump's thread — the piece number, the refusal message,
  and `bytes()` of the buffer rather than the binding's view of it — and queues that. Held to by a
  test asserting nothing on a subscription queue is an alert, rather than by reproducing the crash,
  since a test that segfaults takes the runner with it and reports nothing.

## 0.7.0

### ✨ Features and improvements


### 🐞 Bug fixes

- **The sidecar answers while it is hashing an archive.** Adding a large local archive made
  everything else unreachable for as long as the hash ran: `list` timed out after 60s, the console
  header sat at "connecting…", and clicking an archive never loaded its details. Nothing was wrong
  with any of those calls — the reader loop ran each request to completion before reading the next,
  so they were queued behind one `create`, which for a 698 GiB archive is hours.

  Two things were needed and neither works without the other. `create` now runs on a worker thread,
  and `set_piece_hashes` is passed a progress callback — without one libtorrent holds the GIL for
  the entire hash, so a worker thread would have starved the loop just the same. Measured on
  2.0.13 against 400 MiB: one tick of a 5ms ticker with no callback against thirty-two with one,
  and a longest stall of 580ms against 64ms.

  Replies are matched by id, so answering out of order is what the protocol already expects; a
  write lock keeps two threads from splicing one line-delimited reply into another.

- **`read_piece` no longer blocks everything else either, and alert delivery was reworked so it
  safely can.** This is the one that mattered in normal running: every tile served from a
  cache-mode archive goes through `read_piece`, which waits up to 60s for a piece to arrive from
  the swarm, so a serving node's loop spent most of its life inside one and answered nothing
  meanwhile.

  It could not simply be threaded. Every consumer drained the session's single alert queue and
  dropped what it did not want, so two concurrent reads would each have swallowed the other's
  `read_piece_alert` and both would have timed out — the serial loop that made the sidecar
  unresponsive was also the only thing keeping that correct. One pump thread now pops alerts and
  offers each to whoever subscribed for it, which makes concurrent waiting safe and fixes two
  things on its way past:

  - a read now matches its alert on the **torrent** as well as the piece number. Piece 0 of one
    archive and piece 0 of another are the same index, and draining the queue could not tell them
    apart.
  - a fault is reported **once, by the pump**, rather than only when a read happened to be
    outstanding to notice it. A full disk outlives the request that saw it.

  `reachability` is threaded too, for the same reason: it waits on a `session_stats_alert`.

## 0.6.1

### 🐞 Bug fixes

- **A read no longer cancels the fetch it just asked for.** `set_piece_deadline` with
  `alert_when_available` asks libtorrent to read a piece when it lands — but a piece that is not
  here yet gets read immediately anyway and errors, usually `invalid piece index in slot list`.
  `read_piece` raised on that first errored alert, which meant it both refused to wait and
  abandoned the deadline that would have satisfied it. So every attempt asked for the piece, was
  told "not yet" within milliseconds, and gave up — the caller seeing an instant error rather than
  a read in progress, and the piece never being hurried. On a 698 GiB archive with two complete
  seeds connected, the head was requested every ten minutes and dropped every time, for 200 GiB of
  downloading with the archive still unable to serve a tile. An errored alert is now recorded and
  waited past, the deadline is re-armed (paced, since a missing piece refuses instantly), and the
  caller's own timeout is what ends the wait — which is what the timeout was always for. The last
  refusal is reported with the timeout when one happens.

## 0.6.0

### ✨ Features and improvements

- **The libtorrent sidecar now asks for an archive's head at add time, not only at first read.**
  PMTiles v3 puts a 127-byte header at offset 0 and requires the root directory within the first
  16,384 bytes; those few kilobytes name where every other section begins, so until they are local
  no tile can be located, and once they are, any tile is one targeted range request away. Reads
  already prioritise what they need — every piece fetch raises its piece to 7 with a deadline, the
  header piece included — but that is reactive, and a reader has to exist. When a consumer wrongly
  concluded an archive was already summarised and never issued the read, nothing raised the head
  and the archive sat unservable with no signal that anything was wrong. `add` now raises the
  pieces spanning the first 16 KiB itself, so the head is on its way whether or not anything asks,
  and before the picker has committed to an order. The deadline is the part that matters: priority
  7 promises only that a piece will not be skipped, never when. Cache mode is included — its file
  priorities are 0 so nothing is fetched speculatively, but the head is not speculative — and a
  magnet applies it once its metadata lands. `seedOnly` skips it, the bytes being local already.
  Tunable with `headBytes`, and `prioritise_head` asks for the head of a torrent already added.

### 🐞 Bug fixes

- **Two thirds of the sidecar tests never ran when the file was run the way its own docstring says
  to run it.** A `unittest.main()` block sat in the middle of the module rather than at the end, so
  `python test/test_sidecar_read_piece.py` collected only the classes defined above it and reported
  a confident OK for 10 of 32 tests. `npm run test:sidecar` imports the module and was unaffected,
  which is why it went unnoticed.

## 0.5.2

### ✨ Features and improvements


### 🐞 Bug fixes

- **Removing a torrent with its data now removes its resume file too.** Resume data describes what
  is on disk for a torrent, and it was outliving the data it described. Delete an archive to
  re-fetch it and the record of the old complete file stayed behind, so the re-add handed libtorrent
  a description of a finished 698 GiB archive against a path holding a fresh partial one. libtorrent
  answered with `fastresume_rejected` -- "mismatching file size" -- and set about rechecking, and
  until that settled the torrent held no verified pieces. The visible result was bytes arriving at
  10 MiB/s against a verified-piece count stuck at 1, and every tile read being told, honestly, that
  the piece it wanted was not in the slot list.

  Only when the data goes with it. A removal that keeps the files is how a pause is expressed for an
  engine that has no pause of its own, and the resume file is exactly what makes resuming cheap --
  discarding it there would turn every pause into a full re-hash, which on an 800 GB archive is half
  an hour of disk to rediscover what was already known.


## 0.5.1

### ✨ Features and improvements

- **A `recheck` op, for when the record and the disk disagree.** Every other answer about how much
  of an archive is present is derived from something written down earlier: resume data, or the
  `seed_mode` claim made when it was added. Both can be wrong -- a file replaced underneath the
  session, resume data from a build that was interrupted, a `seed_mode` claim for data that is not
  at the save path -- and when they are, nothing recovers on its own. The torrent sits at 0% beside
  a complete file, downloading what it already has.

  `force_recheck` is the one operation that goes and looks: it discards the stored state, hashes
  every piece against the torrent, and what it finds becomes the truth. A paused torrent is resumed
  first, because a paused torrent does not check and reporting success for a check that never ran
  is worse than refusing.

  Returns as soon as the check is under way rather than waiting for it. A planet archive is tens of
  minutes of disk, which is longer than any sane request timeout; the state to watch is `checking`,
  and progress during it is the fraction hashed.


### 🐞 Bug fixes


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
