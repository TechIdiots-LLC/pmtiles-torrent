#!/usr/bin/env python3
"""
libtorrent sidecar for pmtiles-swarm.

Node has no maintained libtorrent binding — the packages on npm are abandoned
2022 stubs — so this exposes libtorrent over a line-delimited JSON protocol on
stdin/stdout instead. The parent process spawns it and speaks to it over the
pipe; there is no port to secure and the sidecar dies with its parent.

The protocol is deliberately dull, because the point is that it can be
reimplemented behind a real N-API addon later without the Node side noticing:

    -> {"id": 1, "op": "add", "params": {...}}
    <- {"id": 1, "ok": true, "result": {...}}
    <- {"id": 1, "ok": false, "error": "..."}

What libtorrent buys over WebTorrent, and why this exists at all:

  * BitTorrent v2 and hybrid torrents (BEP 52), both creating and joining.
  * Piece-level control — set_piece_deadline, read_piece — which is what
    on-demand tile reads want and which qBittorrent's WebUI does not expose.
  * Resume data, so a restart does not re-hash the entire store.
  * Bulk seeding at multi-terabyte scale.

Requires: python3 and libtorrent (Debian/Ubuntu: apt install python3-libtorrent;
macOS: brew install libtorrent-rasterbar; or pip install libtorrent).
"""

import base64
import json
import os
import sys
import threading
import time

try:
    import libtorrent as lt
except ImportError:  # pragma: no cover - environment dependent
    sys.stderr.write(
        "python3 libtorrent bindings not found. Install python3-libtorrent "
        "(Debian/Ubuntu), libtorrent-rasterbar (Homebrew), or pip install libtorrent.\n"
    )
    sys.exit(2)


# libtorrent reports many states; the parent only distinguishes these.
STATE_MAP = {
    lt.torrent_status.checking_files: "checking",
    lt.torrent_status.downloading_metadata: "downloading",
    lt.torrent_status.downloading: "downloading",
    lt.torrent_status.finished: "seeding",
    lt.torrent_status.seeding: "seeding",
    lt.torrent_status.checking_resume_data: "checking",
}


# Peer flags, looked up by name rather than assumed to exist.
#
# The bindings do not expose the same set across versions — `utp_socket` is in
# the C++ header but absent from the 2.x Python bindings, while `i2p_socket`
# beside it is present. Naming them here means a missing one reads as "this
# build cannot tell me", instead of raising and costing the whole peer list.
PEER_FLAGS = (
    "interesting",
    "choked",
    "remote_interested",
    "remote_choked",
    "supports_extensions",
    "local_connection",
    "handshake",
    "connecting",
    "on_parole",
    "seed",
    "optimistic_unchoke",
    "snubbed",
    "upload_only",
    "endgame_mode",
    "holepunched",
    "utp_socket",
    "rc4_encrypted",
    "plaintext_encrypted",
)


ZERO_HASH = "0" * 40


def _identify(atp):
    """
    The v1 infohash of a torrent that has not been added yet.

    Resume files are named by it, so it has to be readable from whatever the
    caller supplied, before the session has seen either form.

    The metadata is asked first, because `info_hashes` is only filled in for
    params parsed from a magnet. With a .torrent it reads as forty zeros —
    which is a perfectly good name for a file that will never exist, so the
    lookup silently missed and every start re-hashed the whole store.
    """
    info = getattr(atp, "ti", None)
    if info is not None:
        try:
            value = str(info.info_hash())
            if value and value != ZERO_HASH:
                return value
        except Exception:
            pass

    for attribute in ("info_hashes", "info_hash"):
        try:
            holder = getattr(atp, attribute)
            value = str(getattr(holder, "v1", holder))
            if value and value != ZERO_HASH:
                return value
        except Exception:
            continue
    return None


def _bucketise(values, buckets, reduce_fn):
    """
    Reduces a per-piece sequence to a fixed number of buckets.

    Every piece lands in exactly one bucket and every bucket gets at least one
    piece, which matters at both ends: an archive with fewer pieces than
    buckets must not produce empty columns, and one with far more must not drop
    the last few pieces through a rounding gap.
    """
    total = len(values)
    if total == 0:
        return []
    out = []
    for index in range(buckets):
        start = (index * total) // buckets
        stop = max(start + 1, ((index + 1) * total) // buckets)
        out.append(int(reduce_fn(values[start:stop])))
    return out


def _text(value):
    """A str, whichever of bytes or str the bindings handed back."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _has_flag(peer, name):
    """Whether a named peer flag is set, or None where this build lacks it."""
    bit = getattr(lt.peer_info, name, None)
    if bit is None:
        return None
    return bool(peer.flags & bit)


def _transport(peer):
    """
    How the peer is connected: utp, tcp, or unknown.

    Reported as unknown rather than guessed at when the flag is missing. A
    build that cannot answer should say so — "tcp" would be a fact invented to
    fill a column.
    """
    utp = _has_flag(peer, "utp_socket")
    if utp is None:
        return "unknown"
    return "utp" if utp else "tcp"


def _peer_kind(peer):
    """
    Whether this is an ordinary peer, a web seed, or an HTTP seed.

    Worth distinguishing: an archive pulling at full speed from a single web
    seed and one pulling from the swarm look identical in the totals, and only
    the first stops dead when that one server goes away.
    """
    kinds = {
        getattr(lt.peer_info, "standard_bittorrent", object()): "peer",
        getattr(lt.peer_info, "web_seed", object()): "web seed",
        getattr(lt.peer_info, "http_seed", object()): "http seed",
    }
    return kinds.get(peer.connection_type, "peer")


def _peer_flags(peer):
    """Every set flag this build knows about, for the detail view."""
    return [name for name in PEER_FLAGS if _has_flag(peer, name)]


class Sidecar:
    """Owns the libtorrent session and answers requests from the parent."""

    def __init__(self, settings):
        self._lock = threading.Lock()
        self._handles = {}
        self._resume_dir = settings.get("resumeDir")

        pack = {
            "listen_interfaces": settings.get("listen", "0.0.0.0:6881"),
            "alert_mask": lt.alert.category_t.error_notification
            | lt.alert.category_t.storage_notification
            | lt.alert.category_t.status_notification,
            "enable_dht": settings.get("dht", True),
            "enable_lsd": settings.get("lsd", True),
            "enable_upnp": settings.get("upnp", True),
            "enable_natpmp": settings.get("natpmp", True),
        }
        if settings.get("uploadLimit"):
            pack["upload_rate_limit"] = int(settings["uploadLimit"])
        if settings.get("downloadLimit"):
            pack["download_rate_limit"] = int(settings["downloadLimit"])
        # Every peer is a NAT table entry. Consumer routers run out of those
        # long before bandwidth becomes the limit, so this is the knob that
        # decides whether seeding disrupts the rest of the network.
        if settings.get("maxConnections"):
            pack["connections_limit"] = int(settings["maxConnections"])

        self._session = lt.session(pack)
        if self._resume_dir:
            os.makedirs(self._resume_dir, exist_ok=True)

    # ---- operations -----------------------------------------------------

    def op_version(self, _params):
        """Report versions, so the parent can check compatibility."""
        return {"libtorrent": lt.__version__, "python": sys.version.split()[0]}

    def op_add(self, params):
        """Add a torrent from raw .torrent bytes or a magnet URI."""
        save_path = params.get("savePath") or "."
        os.makedirs(save_path, exist_ok=True)

        if params.get("torrentFile"):
            raw = base64.b64decode(params["torrentFile"])
            info = lt.torrent_info(lt.bdecode(raw))
            atp = lt.add_torrent_params()
            atp.ti = info
        else:
            atp = lt.parse_magnet_uri(params["magnet"])

        # Resume data skips re-hashing a store that is already on disk, which
        # for an 800 GB archive is the difference between seconds and half an
        # hour — every single start.
        #
        # Found by the torrent's own infohash rather than one the caller had to
        # remember to send. It did rely on the caller, nothing sent it, so the
        # lookup was always for None and every restart re-checked everything.
        resume = self._read_resume(params.get("infoHash") or _identify(atp))
        if resume:
            restored = lt.read_resume_data(resume)
            # Resume data does not carry the metadata, so a .torrent add keeps
            # the metainfo it already parsed. Without this a resumed torrent
            # has no file list until it fetches one over BEP 9 — from a swarm,
            # for a file already on the disk.
            if atp.ti is not None:
                restored.ti = atp.ti
            atp = restored

        # Applied after the resume swap, not before: read_resume_data returns a
        # fresh params object, so anything set earlier was thrown away. Cache
        # mode losing its file priorities that way meant a resumed cache-mode
        # archive quietly began downloading all of it.
        atp.save_path = save_path

        # Cache mode joins the swarm without committing the disk to a full
        # copy. This must be file priority 0 rather than upload_mode: upload
        # mode refuses to download anything at all, which would break the whole
        # point, because read_piece raises an individual piece back to priority
        # 7 to fetch it on demand. Priority 0 means "do not fetch this
        # proactively", which still allows that.
        cache_mode = params.get("mode") == "cache"
        if cache_mode:
            if atp.ti is not None:
                atp.file_priorities = [0] * atp.ti.num_files()
            # A torrent with nothing wanted looks idle to the auto-manager,
            # which pauses it — and a paused torrent stops seeding. Cache mode
            # must stay active precisely so it can serve the pieces it holds.
            atp.flags &= ~lt.torrent_flags.auto_managed
            atp.flags &= ~lt.torrent_flags.paused

        if params.get("paused"):
            atp.flags |= lt.torrent_flags.paused

        handle = self._session.add_torrent(atp)

        # A magnet has no metadata yet, so file priorities cannot be set until
        # it arrives. Apply them once it does, otherwise a cache-mode magnet
        # would quietly start downloading the whole archive.
        if cache_mode and atp.ti is None:
            threading.Thread(
                target=self._deprioritise_when_ready, args=(handle,), daemon=True
            ).start()

        info_hash = str(handle.info_hash())
        with self._lock:
            self._handles[info_hash] = handle
        return {"infoHash": info_hash}

    def _deprioritise_when_ready(self, handle, timeout=300):
        """Set every file to priority 0 once metadata arrives."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if handle.has_metadata():
                try:
                    handle.prioritize_files([0] * handle.torrent_file().num_files())
                except Exception:  # noqa: BLE001 - best effort
                    pass
                return
            time.sleep(1)

    def op_remove(self, params):
        """Remove a torrent, optionally deleting its data."""
        handle = self._handle(params["infoHash"])
        flags = lt.options_t.delete_files if params.get("deleteData") else 0
        self._session.remove_torrent(handle, flags)
        with self._lock:
            self._handles.pop(params["infoHash"], None)
        return {"removed": True}

    def op_list(self, _params):
        """Report the state of every torrent in the session."""
        return [self._status(h) for h in self._session.get_torrents()]

    def op_get(self, params):
        """Report one torrent's state."""
        try:
            return self._status(self._handle(params["infoHash"]))
        except KeyError:
            return None

    def op_trackers(self, params):
        """Report each tracker and how its last announce went.

        A torrent that finds no peers is otherwise indistinguishable from a
        swarm that has none: the status says "downloading, 0 peers" either way.
        libtorrent knows the difference — it has the announce result for every
        tracker, including why one failed — and this is the only way to see it.
        """
        handle = self._handle(params["infoHash"])
        out = []
        for entry in handle.trackers():
            # The per-endpoint records carry the real error for a tracker that
            # answered differently over IPv4 and IPv6.
            endpoints = entry.get("endpoints") or []
            messages = [
                text
                for text in (
                    str(endpoint.get("message") or "") for endpoint in endpoints
                )
                if text
            ]
            errors = [
                str(endpoint.get("last_error") or "")
                for endpoint in endpoints
                if endpoint.get("last_error")
            ]

            out.append({
                "url": entry.get("url"),
                "tier": entry.get("tier"),
                "fails": entry.get("fails"),
                "verified": bool(entry.get("verified")),
                "updating": bool(entry.get("updating")),
                "message": str(entry.get("message") or "") or (messages[0] if messages else ""),
                "lastError": str(entry.get("last_error") or "") or (errors[0] if errors else ""),
                "nextAnnounce": str(entry.get("next_announce") or ""),
            })
        return {"trackers": out}

    def op_pieces(self, params):
        """
        Which pieces are held, how rare each one is, and what peers have.

        Downsampled to `buckets` before it leaves, because full resolution does
        not survive the trip: a 698 GiB archive at 4 MiB pieces is 178,000 of
        them, and one byte each is a quarter-megabyte of JSON per poll. A bar
        on a screen is a thousand columns wide at most, so the reduction costs
        nothing that could have been displayed.

        Availability reduces by **minimum** rather than average on purpose. The
        question a person asks of that bar is "can I still complete this", and
        one piece nobody has is the answer regardless of how well supplied its
        neighbours are — an average would hide exactly the case worth seeing.
        """
        handle = self._handle(params["infoHash"])
        status = handle.status()
        if not status.has_metadata:
            raise RuntimeError("metadata has not arrived yet")

        info = handle.torrent_file()
        total = info.num_pieces()
        buckets = int(params.get("buckets") or 0) or total
        buckets = max(1, min(buckets, total))

        have = list(status.pieces or [])
        # `have` reduces by minimum too: a bucket is only "complete" when every
        # piece in it is, so a nearly-full bar cannot be mistaken for a done one.
        held = _bucketise(have, buckets, lambda values: 1 if all(values) else 0)

        # Peers are read once, up here, because availability is derived from
        # them when libtorrent will not answer directly.
        peer_infos = handle.get_peer_info()

        try:
            availability = list(handle.piece_availability())
        except Exception:
            availability = []

        if not availability:
            # `piece_availability()` returns nothing on a session that has not
            # been asked to post it — the supported route in 2.x is an alert,
            # which this synchronous protocol has no good place to wait for.
            # Counting the connected peers' own bitfields gives the same
            # quantity from data already in hand: how many peers hold each
            # piece. It sees only connected peers rather than everything the
            # swarm knows of, which is a smaller sample of the same thing.
            availability = [0] * total
            for peer in peer_infos:
                try:
                    bits = peer.pieces
                except Exception:
                    continue
                for index in range(min(total, len(bits))):
                    if bits[index]:
                        availability[index] += 1

        rare = (
            _bucketise(availability, buckets, lambda values: min(min(values), 255))
            if any(availability)
            else []
        )

        # qBittorrent's "Availability: 1.603" — how many whole copies the swarm
        # holds between it. libtorrent's own figure is preferred; when it is
        # not reporting one, the same quantity is derived from the availability
        # counted above, so the number never sits at zero beside a bar that
        # plainly has data in it.
        copies = max(0.0, float(status.distributed_copies))
        if copies == 0.0 and any(availability):
            floor = min(availability)
            beyond = sum(1 for count in availability if count > floor)
            copies = floor + beyond / len(availability)

        out = {
            "numPieces": total,
            "pieceLength": info.piece_length(),
            "buckets": buckets,
            "have": base64.b64encode(bytes(held)).decode("ascii"),
            "availability": base64.b64encode(bytes(rare)).decode("ascii") if rare else None,
            "distributedCopies": copies,
            "haveCount": int(status.num_pieces),
        }

        if params.get("peers"):
            peers = []
            for peer in peer_infos:
                try:
                    bits = list(peer.pieces)
                except Exception:
                    continue
                peers.append(
                    {
                        "address": f"{peer.ip[0]}:{peer.ip[1]}",
                        "client": _text(peer.client),
                        # Per peer the useful reduction is "any", not "all":
                        # this bar answers "where could I get pieces from", and
                        # a peer holding part of a bucket can still serve it.
                        "have": base64.b64encode(
                            bytes(_bucketise(bits, buckets, lambda values: 1 if any(values) else 0))
                        ).decode("ascii"),
                        "progress": peer.progress,
                    }
                )
            out["peers"] = peers

        return out

    def op_rate_limits(self, params):
        """
        Set the session's global rate limits, in bytes per second.

        Applied to the running session rather than at startup, because the
        point of a schedule is to change the limit at a particular hour — a
        setting that only took effect on restart could not do that at all.

        libtorrent takes 0 for unlimited; the caller's -1 means the same thing,
        so both map onto 0 here. Negative values other than -1 would be a
        caller bug, and clamping them to unlimited is the safe reading: the
        alternative is a session throttled to a rate nobody asked for.
        """
        settings = {}
        for key, name in (("download", "download_rate_limit"), ("upload", "upload_rate_limit")):
            if key not in params:
                continue
            rate = int(params[key])
            settings[name] = 0 if rate < 0 else rate

        if settings:
            self._session.apply_settings(settings)

        current = self._session.get_settings()
        return {
            "download": current.get("download_rate_limit", 0),
            "upload": current.get("upload_rate_limit", 0),
        }

    def op_peers(self, params):
        """
        Report per-peer detail for one torrent.

        Built one field at a time and per peer, because the alternative failed
        badly: `peer_info.utp_socket` does not exist in the 2.x Python
        bindings, so reading it raised on the first peer and the caller got an
        empty list — a swarm downloading at 10 MiB/s reported as having no
        peers at all. One optional attribute should cost that attribute, not
        the answer.
        """
        handle = self._handle(params["infoHash"])
        out = []
        for peer in handle.get_peer_info():
            entry = {}
            for key, read in (
                ("address", lambda p: f"{p.ip[0]}:{p.ip[1]}"),
                ("client", lambda p: _text(p.client)),
                ("progress", lambda p: p.progress),
                ("downloadSpeed", lambda p: p.down_speed),
                ("uploadSpeed", lambda p: p.up_speed),
                ("connection", _transport),
                ("kind", _peer_kind),
                ("flags", _peer_flags),
            ):
                try:
                    entry[key] = read(peer)
                except Exception:
                    # An attribute this build does not expose. Skipped, so the
                    # rest of the peer still arrives.
                    pass
            out.append(entry)
        return out

    def op_create(self, params):
        """
        Create a torrent from a local file.

        Defaults to a hybrid v1+v2 torrent: v2 brings per-file merkle trees
        with 16 KiB leaf blocks, so a peer can verify a small block without the
        whole hash list, while the v1 half keeps every existing client working.
        This is the main thing create-torrent cannot do.
        """
        path = params["path"]
        piece_length = int(params.get("pieceLength", 4 * 1024 * 1024))

        storage = lt.file_storage()
        lt.add_files(storage, path)

        flags = 0
        fmt = params.get("format", "hybrid")
        if fmt == "v1":
            flags |= lt.create_torrent.v1_only
        elif fmt == "v2":
            flags |= lt.create_torrent.v2_only

        creator = lt.create_torrent(storage, piece_length, flags=flags)

        # Trackers arrive as BEP 12 tiers: a list of lists, tried in order,
        # everything within a tier tried together. Flattening them would turn a
        # deliberate fallback order into a stampede.
        for tier, group in enumerate(params.get("trackers", [])):
            for tracker in group if isinstance(group, list) else [group]:
                creator.add_tracker(tracker, tier)
        for seed in params.get("webSeeds", []):
            creator.add_url_seed(seed)
        if params.get("comment"):
            creator.set_comment(params["comment"])
        if params.get("private"):
            creator.set_priv(True)
        creator.set_creator(params.get("createdBy") or "pmtiles-swarm")

        lt.set_piece_hashes(creator, os.path.dirname(path) or ".")
        entry = creator.generate()
        raw = lt.bencode(entry)
        info = lt.torrent_info(entry)

        return {
            "torrentFile": base64.b64encode(raw).decode(),
            "infoHash": str(info.info_hash()),
            "name": info.name(),
            "size": info.total_size(),
            "pieceLength": info.piece_length(),
            "pieceCount": info.num_pieces(),
            "format": fmt,
        }

    def op_read_piece(self, params):
        """
        Read one piece, prioritising it ahead of everything else.

        set_piece_deadline is the primitive that makes on-demand tile serving
        work: it promotes a piece to the front of the queue rather than waiting
        for the normal picker. qBittorrent's API has no equivalent.
        """
        handle = self._handle(params["infoHash"])
        index = int(params["piece"])
        deadline = int(params.get("deadlineMs", 1000))

        handle.piece_priority(index, 7)
        handle.set_piece_deadline(index, deadline, lt.deadline_flags_t.alert_when_available)

        timeout = time.time() + float(params.get("timeoutMs", 60000)) / 1000
        while time.time() < timeout:
            alert = self._session.wait_for_alert(500)
            if alert is None:
                continue
            for item in self._session.pop_alerts():
                if isinstance(item, lt.read_piece_alert) and item.piece == index:
                    if item.error.value() != 0:
                        raise RuntimeError(f"piece {index}: {item.error.message()}")
                    return {"piece": index, "data": base64.b64encode(item.buffer).decode()}
        raise TimeoutError(f"timed out waiting for piece {index}")

    def op_set_priority(self, params):
        """
        Set the download priority of a piece range.

        This is what background hydration is built on: a range at priority 1
        is fetched when there is nothing more urgent, and dropping it back to 0
        stops it competing the moment a real read arrives. libtorrent exposes
        this per piece; qBittorrent's API has nothing equivalent, which is why
        it cannot do on-demand reads properly.
        """
        handle = self._handle(params["infoHash"])
        first = int(params["first"])
        last = int(params["last"])
        priority = int(params.get("priority", 0))

        for index in range(first, last + 1):
            handle.piece_priority(index, priority)
            if priority == 0:
                # Clear any deadline too, or a previously urgent piece keeps
                # its place in the queue despite having no priority.
                handle.reset_piece_deadline(index)
        return {"pieces": last - first + 1, "priority": priority}

    def op_info(self, params):
        """Report the geometry a reader needs to map byte ranges onto pieces."""
        handle = self._handle(params["infoHash"])
        if not handle.status().has_metadata:
            raise RuntimeError("metadata has not arrived yet")

        info = handle.torrent_file()
        index = int(params.get("fileIndex", 0))
        entry = info.files()

        return {
            "infoHash": str(handle.info_hash()),
            "pieceLength": info.piece_length(),
            "numPieces": info.num_pieces(),
            "name": entry.file_name(index),
            "fileLength": entry.file_size(index),
            "fileOffset": entry.file_offset(index),
            "numFiles": info.num_files(),
        }

    def op_save_resume(self, params):
        """Persist resume data for one torrent, or all of them."""
        handles = (
            [self._handle(params["infoHash"])]
            if params.get("infoHash")
            else self._session.get_torrents()
        )
        saved = 0
        for handle in handles:
            if handle.need_save_resume_data():
                handle.save_resume_data()
                saved += 1
        # Alerts carry the actual data; drain briefly to collect them.
        deadline = time.time() + 5
        while saved > 0 and time.time() < deadline:
            self._session.wait_for_alert(500)
            for alert in self._session.pop_alerts():
                if isinstance(alert, lt.save_resume_data_alert):
                    self._write_resume(str(alert.handle.info_hash()), alert.params)
                    saved -= 1
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    saved -= 1
        return {"saved": True}

    def op_shutdown(self, _params):
        """Persist resume data and stop."""
        try:
            self.op_save_resume({})
        finally:
            del self._session
        return {"stopped": True}

    # ---- helpers --------------------------------------------------------

    def _handle(self, info_hash):
        with self._lock:
            handle = self._handles.get(info_hash)
        if handle is None:
            for candidate in self._session.get_torrents():
                if str(candidate.info_hash()) == info_hash:
                    return candidate
            raise KeyError(f"no such torrent: {info_hash}")
        return handle

    def _status(self, handle):
        s = handle.status()

        # torrent_status.paused is deprecated in libtorrent 2.x and reports
        # unreliably; the live value lives on the handle's flags.
        paused = bool(handle.flags() & lt.torrent_flags.paused)

        # A torrent whose files are all priority 0 is in cache mode: joined and
        # seeding whatever it holds, but fetching nothing on its own. Reporting
        # that as "paused" would be misleading — it is working as intended.
        has_metadata = s.has_metadata
        cache_mode = has_metadata and s.total_wanted == 0

        if paused:
            state = "paused"
        elif cache_mode:
            state = "cache"
        else:
            state = STATE_MAP.get(s.state, "stalled")

        return {
            "infoHash": str(handle.info_hash()),
            "name": s.name,
            # total_wanted is zero in cache mode, so report the real size.
            "size": handle.torrent_file().total_size() if has_metadata else s.total_wanted,
            "progress": s.progress,
            "state": state,
            # Connected clients, not swarm size — and never this one, since a
            # client is not its own peer. So a fully seeded archive nobody is
            # currently downloading reads 0 peers, which is correct and says
            # nothing is wrong.
            "peers": max(0, s.num_peers - s.num_seeds),
            "seeds": s.num_seeds,
            # What the tracker last said the whole swarm holds, this node
            # included. It is the difference between "nobody wants this" and
            # "nobody knows about it", which the connected counts alone cannot
            # tell apart. -1 until a tracker has answered a scrape.
            "swarmSeeds": s.num_complete,
            "swarmPeers": s.num_incomplete,
            "downloadSpeed": s.download_rate,
            "uploadSpeed": s.upload_rate,
            "downloaded": s.total_done,
            "uploaded": s.all_time_upload,
            "ratio": (s.all_time_upload / s.total_done) if s.total_done else 0,
            "savePath": s.save_path,
        }

    def _resume_path(self, info_hash):
        if not self._resume_dir or not info_hash:
            return None
        return os.path.join(self._resume_dir, f"{info_hash}.resume")

    def _read_resume(self, info_hash):
        path = self._resume_path(info_hash)
        if not path or not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def _write_resume(self, info_hash, params):
        path = self._resume_path(info_hash)
        if not path:
            return
        with open(path, "wb") as handle:
            handle.write(lt.write_resume_data_buf(params))


def main():
    """Read requests from stdin, write responses to stdout, one JSON per line."""
    settings = json.loads(os.environ.get("SIDECAR_SETTINGS", "{}"))
    sidecar = Sidecar(settings)

    # Announce readiness so the parent does not have to guess.
    print(json.dumps({"event": "ready", "libtorrent": lt.__version__}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            print(json.dumps({"id": None, "ok": False, "error": f"bad request: {error}"}), flush=True)
            continue

        request_id = request.get("id")
        op = request.get("op", "")
        handler = getattr(sidecar, f"op_{op}", None)
        if handler is None:
            print(json.dumps({"id": request_id, "ok": False, "error": f"unknown op: {op}"}), flush=True)
            continue

        try:
            result = handler(request.get("params") or {})
            print(json.dumps({"id": request_id, "ok": True, "result": result}), flush=True)
        except Exception as error:  # noqa: BLE001 - report everything to the parent
            print(json.dumps({"id": request_id, "ok": False, "error": str(error)}), flush=True)

        if op == "shutdown":
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Windows delivers a console Ctrl-C to every process in the group, so
        # this arrives here as well as in the parent — usually while blocked
        # reading stdin. The parent is already shutting us down deliberately;
        # printing a traceback on the way out makes an ordinary stop look like
        # a crash, and buries the lines that say what actually happened.
        pass
    except BrokenPipeError:
        # The parent closed the pipe before we finished writing. Same story:
        # it is how a stop looks from this end, not a fault.
        pass
