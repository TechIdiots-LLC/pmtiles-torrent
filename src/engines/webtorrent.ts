import type {
  Priority,
  ReadRangeOptions,
  TorrentEngine,
  TorrentInfo,
} from "../types.js";

/**
 * Structural types for the small slice of WebTorrent's API we use. Declaring
 * them here keeps `@types/webtorrent` out of the dependency tree and means this
 * adapter compiles whether or not `webtorrent` is installed.
 *
 * These describe the public API of WebTorrent (MIT, Copyright (c) Feross
 * Aboukhadijeh and WebTorrent, LLC — https://github.com/webtorrent/webtorrent).
 * No WebTorrent implementation code is copied here. See NOTICE.md.
 */
interface WtFile {
  name: string;
  path: string;
  length: number;
  offset: number;
  [Symbol.asyncIterator](opts?: {
    start?: number;
    end?: number;
  }): AsyncIterator<Uint8Array> & { return?(): Promise<unknown> };
}

interface WtTorrent {
  infoHash: string;
  name: string;
  pieceLength: number;
  lastPieceLength: number;
  pieces: unknown[];
  files: WtFile[];
  path: string;
  ready: boolean;
  destroyed: boolean;
  bitfield?: { buffer: Uint8Array };
  select(start: number, end: number, priority?: number): void;
  deselect(start: number, end: number): void;
  critical(start: number, end: number): void;
  destroy(opts?: Record<string, unknown>): void;
  on(event: string, handler: (...args: unknown[]) => void): void;
  once(event: string, handler: (...args: unknown[]) => void): void;
  removeListener(event: string, handler: (...args: unknown[]) => void): void;
}

interface WtClient {
  add(
    torrentId: unknown,
    opts?: Record<string, unknown>,
    ontorrent?: (torrent: WtTorrent) => void,
  ): WtTorrent;
  destroy(cb?: (err?: Error) => void): void;
}

export interface WebTorrentEngineOptions {
  /**
   * Reuse an existing WebTorrent client. Strongly recommended when serving more
   * than one archive: one client means one peer pool, one port, one DHT node.
   * When supplied, `destroy()` removes only this torrent and leaves the client
   * running.
   *
   * May be a factory, called on first read, so a host application can share one
   * client without constructing it until an archive actually needs it.
   */
  client?: WtClient | (() => WtClient | Promise<WtClient>);
  /** Options passed to `new WebTorrent()` when this engine creates the client. */
  clientOptions?: Record<string, unknown>;
  /** Download path for the chunk store. Persist this to keep seeding across restarts. */
  path?: string;
  /** Extra tracker announce URLs. */
  announce?: string[];
  /**
   * Select a specific file in a multi-file torrent by its path. Without it the
   * engine picks the largest `.pmtiles` file, falling back to the largest file.
   */
  filePath?: string;
  /**
   * How long to wait for torrent metadata. Default 60s, plus an allowance that
   * scales with the piece count where the metainfo is supplied outright.
   */
  readyTimeoutMs?: number;
  /**
   * Directory for resume data. WebTorrent otherwise re-hashes the entire store
   * on every start to rebuild its bitfield, which on a 72 GiB archive costs
   * about a minute and scales with size. Saving the bitfield reduces that to
   * milliseconds.
   */
  resumePath?: string;
  /** How often to persist resume data while running. Default 60s. */
  resumeIntervalMs?: number;
  /**
   * Simultaneous connections per web seed. WebTorrent defaults to 4; raise it
   * when the torrent carries a BEP 19 url-list, since a web seed is usually far
   * faster and more available than the swarm.
   */
  maxWebConns?: number;
}

const PRIORITY_VALUES: Record<Priority, number> = {
  critical: 10,
  high: 5,
  normal: 0,
};

function abortError(): Error {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

/** Rejects as soon as a signal aborts, rather than waiting for the promise. */
function raceAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

/** Pull an infohash out of a magnet URI so `getKey()` works before metadata. */
function deriveKey(torrentId: unknown): string {
  if (typeof torrentId !== "string") return "torrent:unknown";
  const magnet = /xt=urn:bt[im]h:([a-z0-9]+)/i.exec(torrentId);
  if (magnet) return `torrent:${magnet[1].toLowerCase()}`;
  if (/^[a-f0-9]{40}$/i.test(torrentId)) {
    return `torrent:${torrentId.toLowerCase()}`;
  }
  return `torrent:${torrentId}`;
}

/** Chooses which file in the torrent is the PMTiles archive. */
function pickFile(torrent: WtTorrent, filePath?: string): WtFile {
  if (torrent.files.length === 0) throw new Error("torrent contains no files");

  if (filePath) {
    const match = torrent.files.find(
      (file) => file.path === filePath || file.name === filePath,
    );
    if (!match) {
      throw new Error(
        `no file "${filePath}" in torrent (has: ${torrent.files
          .map((f) => f.path)
          .join(", ")})`,
      );
    }
    return match;
  }

  const byLength = [...torrent.files].sort((a, b) => b.length - a.length);
  return byLength.find((file) => file.name.endsWith(".pmtiles")) ?? byLength[0];
}

/** Format version for resume files, so a change can invalidate old ones. */
const RESUME_VERSION = 1;

interface ResumeData {
  version: number;
  infoHash: string;
  numPieces: number;
  dataFile: string;
  dataSize: number;
  dataMtimeMs: number;
  bitfield: Uint8Array;
}

/** Path of the resume file for a source key. */
async function resumeFilePath(resumePath: string, key: string): Promise<string> {
  const path = await import("node:path");
  const safe = key.replace(/[^a-z0-9]+/gi, "_").slice(0, 120);
  return path.join(resumePath, `${safe}.resume.json`);
}

/**
 * Loads resume data, if it is still valid for what is on disk.
 *
 * A bitfield claims pieces are present without re-hashing them, so a stale one
 * would have us serve unverified bytes. It is therefore only trusted when the
 * data file is exactly the size and modification time it had when the bitfield
 * was written — any write to the file invalidates it, and the cost of being
 * wrong is one slow startup rather than corrupt tiles.
 */
async function loadResume(
  resumePath: string,
  key: string,
  dataPath: string,
): Promise<ResumeData | null> {
  try {
    const [fs, path] = await Promise.all([
      import("node:fs/promises"),
      import("node:path"),
    ]);
    const file = await resumeFilePath(resumePath, key);
    const saved = JSON.parse(await fs.readFile(file, "utf8"));
    if (saved.version !== RESUME_VERSION) return null;
    if (!saved.dataFile || !saved.bitfield) return null;

    const stat = await fs.stat(path.join(dataPath, saved.dataFile));
    if (stat.size !== saved.dataSize) return null;
    if (Math.floor(stat.mtimeMs) !== Math.floor(saved.dataMtimeMs)) return null;

    return {
      ...saved,
      bitfield: new Uint8Array(Buffer.from(saved.bitfield, "base64")),
    };
  } catch {
    // Missing or unreadable resume data just means a cold start.
    return null;
  }
}

/** Persists the bitfield alongside the identity of the data it describes. */
async function saveResume(
  resumePath: string,
  key: string,
  torrent: WtTorrent,
  file: WtFile,
): Promise<void> {
  try {
    const [fs, path] = await Promise.all([
      import("node:fs/promises"),
      import("node:path"),
    ]);
    const bitfield = torrent.bitfield?.buffer;
    if (!bitfield) return;

    const dataFile = file.path;
    const stat = await fs.stat(path.join(torrent.path, dataFile));

    await fs.mkdir(resumePath, { recursive: true });
    const target = await resumeFilePath(resumePath, key);
    const body = JSON.stringify({
      version: RESUME_VERSION,
      infoHash: torrent.infoHash,
      numPieces: torrent.pieces.length,
      dataFile,
      dataSize: stat.size,
      dataMtimeMs: Math.floor(stat.mtimeMs),
      bitfield: Buffer.from(bitfield).toString("base64"),
    });
    // Write then rename, so a crash cannot leave a half-written bitfield.
    await fs.writeFile(`${target}.tmp`, body);
    await fs.rename(`${target}.tmp`, target);
  } catch {
    // Resume data is an optimisation; failing to save it is not fatal.
  }
}

type WebTorrentConstructor = new (opts?: Record<string, unknown>) => unknown;

async function loadWebTorrent(): Promise<WebTorrentConstructor> {
  try {
    // Indirect specifier on purpose: it keeps `webtorrent` a genuinely optional
    // dependency (no types required to compile) and stops bundlers from pulling
    // a Node-flavoured torrent client into browser builds that never use it.
    const specifier = "webtorrent";
    const mod = (await import(specifier)) as {
      default?: WebTorrentConstructor;
    };
    const ctor = mod.default ?? (mod as unknown as WebTorrentConstructor);
    if (typeof ctor !== "function") {
      throw new Error("webtorrent module did not export a constructor");
    }
    return ctor;
  } catch (error) {
    throw new Error(
      "WebTorrentEngine requires the optional peer dependency 'webtorrent'. " +
        "Install it, or pass an existing client via options.client. " +
        `(${(error as Error).message})`,
      { cause: error },
    );
  }
}


/**
 * How many v1 pieces a bencoded metainfo declares, without parsing it.
 *
 * Reads the length prefix of the `pieces` string, which is 20 bytes per piece.
 * Enough to size a timeout, and cheaper than a bencode parser this package
 * does not otherwise need. Returns 0 for a magnet, which carries no such thing.
 */
function pieceCountOf(torrentId: unknown): number {
  if (!(torrentId instanceof Uint8Array)) return 0;
  const marker = [54, 58, 112, 105, 101, 99, 101, 115]; // "6:pieces"
  outer: for (let at = 0; at + marker.length < torrentId.length; at++) {
    for (let n = 0; n < marker.length; n++) {
      if (torrentId[at + n] !== marker[n]) continue outer;
    }
    let digits = "";
    for (let n = at + marker.length; n < torrentId.length; n++) {
      const code = torrentId[n];
      if (code === 58) break; // ":"
      if (code < 48 || code > 57) return 0;
      digits += String.fromCharCode(code);
    }
    return digits ? Math.floor(Number(digits) / 20) : 0;
  }
  return 0;
}
/**
 * A {@link TorrentEngine} backed by WebTorrent.
 *
 * Works in Node and in the browser, and is the only engine that can bridge the
 * two: browser peers speak WebRTC and conventional clients speak TCP/uTP, so a
 * WebTorrent-based server is what lets one swarm serve both.
 *
 * Note this is a BitTorrent v1 engine — WebTorrent does not implement BEP 52,
 * so v2 merkle verification and `btmh` magnets need a different engine.
 */
export class WebTorrentEngine implements TorrentEngine {
  readonly key: string;

  #torrentId: unknown;
  #options: WebTorrentEngineOptions;
  #client?: WtClient;
  #torrent?: WtTorrent;
  #file?: WtFile;
  #ownsClient = false;
  #ownsTorrent = true;
  #readyPromise?: Promise<TorrentInfo>;
  #destroyed = false;
  #resumeTimer?: ReturnType<typeof setInterval>;

  constructor(torrentId: unknown, options: WebTorrentEngineOptions = {}) {
    this.#torrentId = torrentId;
    this.#options = options;
    this.key = deriveKey(torrentId);
  }

  /** The underlying torrent, once metadata has arrived. For swarm stats. */
  get torrent(): WtTorrent | undefined {
    return this.#torrent;
  }

  ready(): Promise<TorrentInfo> {
    if (!this.#readyPromise) this.#readyPromise = this.#start();
    return this.#readyPromise;
  }

  async readRange(
    offset: number,
    length: number,
    options: ReadRangeOptions = {},
  ): Promise<Uint8Array> {
    await this.ready();
    const file = this.#file as WtFile;
    const { signal } = options;
    if (signal?.aborted) throw abortError();

    const end = offset + length - 1;
    // WebTorrent's iterator selects the range at elevated priority and marks
    // pieces critical as it advances, which is exactly the behaviour we want
    // for a blocking read. Driving `next()` by hand (rather than `for await`)
    // is what lets an abort interrupt a stalled fetch instead of waiting for
    // the next chunk to arrive.
    const iterator = file[Symbol.asyncIterator]({ start: offset, end });

    const out = new Uint8Array(length);
    let written = 0;
    try {
      while (written < length) {
        const result = await raceAbort(
          Promise.resolve(iterator.next()),
          signal,
        );
        if (result.done) break;
        const chunk = result.value;
        const take = Math.min(chunk.byteLength, length - written);
        out.set(chunk.subarray(0, take), written);
        written += take;
      }
    } finally {
      // Releases the iterator's piece selection whether we finished or bailed.
      try {
        await iterator.return?.();
      } catch {
        /* the read already failed; nothing useful to do here */
      }
    }

    if (written < length) {
      throw new Error(
        `short read from torrent: got ${written} of ${length} bytes at offset ${offset}`,
      );
    }
    return out;
  }

  hint(offset: number, length: number, priority: Priority): void {
    const torrent = this.#torrent;
    const file = this.#file;
    if (!torrent || !file || torrent.destroyed || length <= 0) return;

    const first = Math.floor((file.offset + offset) / torrent.pieceLength);
    const last = Math.floor(
      (file.offset + offset + length - 1) / torrent.pieceLength,
    );
    torrent.select(first, last, PRIORITY_VALUES[priority]);
    if (priority === "critical") torrent.critical(first, last);
  }

  /**
   * Withdraws a previous hint, so the range stops competing for bandwidth.
   *
   * This only clears non-streaming selections, which is what `hint()` creates —
   * the selections an in-flight read makes for itself are untouched.
   */
  unhint(offset: number, length: number): void {
    const torrent = this.#torrent;
    const file = this.#file;
    if (!torrent || !file || torrent.destroyed || length <= 0) return;

    const first = Math.floor((file.offset + offset) / torrent.pieceLength);
    const last = Math.floor(
      (file.offset + offset + length - 1) / torrent.pieceLength,
    );
    torrent.deselect(first, last);
  }

  async destroy(): Promise<void> {
    if (this.#destroyed) return;
    this.#destroyed = true;

    if (this.#resumeTimer !== undefined) {
      clearInterval(this.#resumeTimer);
      this.#resumeTimer = undefined;
    }
    // Capture the bitfield before tearing anything down; this is the save that
    // makes the next start fast.
    if (this.#options.resumePath && this.#options.path && this.#torrent) {
      await saveResume(
        this.#options.resumePath,
        this.key,
        this.#torrent,
        this.#file as WtFile,
      );
    }

    const torrent = this.#torrent;
    const client = this.#client;
    this.#torrent = undefined;
    this.#file = undefined;
    this.#client = undefined;
    this.#readyPromise = undefined;

    if (this.#ownsClient && client) {
      await new Promise<void>((resolve) => client.destroy(() => resolve()));
      return;
    }
    // Shared client: drop our torrent but leave the client (and its other
    // torrents) alone. Keep the store so we can resume seeding later. A torrent
    // we joined rather than added belongs to someone else, so leave it be.
    if (this.#ownsTorrent && torrent && !torrent.destroyed) {
      torrent.destroy({ destroyStore: false });
    }
  }

  /**
   * Periodically persists resume data, so a crash costs at most one interval
   * of re-hashing rather than the whole store.
   */
  #startResumeTimer(): void {
    if (!this.#options.resumePath || !this.#options.path) return;
    const interval = this.#options.resumeIntervalMs ?? 60000;
    this.#resumeTimer = setInterval(() => {
      if (this.#torrent && this.#file) {
        void saveResume(
          this.#options.resumePath as string,
          this.key,
          this.#torrent,
          this.#file,
        );
      }
    }, interval);
    this.#resumeTimer.unref?.();
  }

  /** Removes resume data that turned out not to describe this torrent. */
  async #discardResume(): Promise<void> {
    try {
      const fs = await import("node:fs/promises");
      const target = await resumeFilePath(
        this.#options.resumePath as string,
        this.key,
      );
      await fs.rm(target, { force: true });
    } catch {
      /* nothing useful to do if it cannot be removed */
    }
  }

  async #start(): Promise<TorrentInfo> {
    if (this.#destroyed) throw new Error("engine is destroyed");

    const provided = this.#options.client;
    let client: WtClient;
    if (provided) {
      client = typeof provided === "function" ? await provided() : provided;
    } else {
      const WebTorrent = await loadWebTorrent();
      client = new WebTorrent(this.#options.clientOptions ?? {}) as WtClient;
      this.#ownsClient = true;
    }
    this.#client = client;

    const addOptions: Record<string, unknown> = {
      // Without this WebTorrent selects every piece and starts downloading the
      // whole archive. We want on-demand ranges only — the point of the
      // exercise is serving a 700 GiB map without holding 700 GiB.
      deselect: true,
    };
    if (this.#options.path) addOptions.path = this.#options.path;
    else {
      // Nothing is persisted, so there is nothing a verify pass could find.
      // It is not free either: WebTorrent walks every piece before `ready`
      // fires, which on a 178,000-piece archive is what a browser spends its
      // whole metadata budget on. See the README — "In a browser".
      addOptions.skipVerify = true;
    }
    if (this.#options.announce) addOptions.announce = this.#options.announce;
    // If the torrent carries a BEP 19 url-list, that HTTP origin is usually
    // faster and far more available than the swarm — measured serving a tile in
    // under a second with DHT and trackers disabled entirely. WebTorrent allows
    // only 4 simultaneous connections per web seed by default, which throttles
    // exactly the case worth leaning on.
    if (this.#options.maxWebConns) {
      addOptions.maxWebConns = this.#options.maxWebConns;
    }

    // Resume data, when it is still valid, replaces a full re-hash of the
    // store. WebTorrent ignores a bitfield whose byte length does not match
    // the piece count, so a mismatched one degrades to a normal verify.
    let resume: ResumeData | null = null;
    if (this.#options.resumePath && this.#options.path) {
      resume = await loadResume(
        this.#options.resumePath,
        this.key,
        this.#options.path,
      );
      if (resume) addOptions.bitfield = resume.bitfield;
    }

    // A bigger torrent takes longer to bring up, and a fixed budget silently
    // excludes the archives most worth sharing. One millisecond per piece on
    // top of the base, which is generous for parsing and cheap to be wrong
    // about — the timeout only ever ends a wait that was going to fail.
    const pieces = pieceCountOf(this.#torrentId);
    const timeoutMs = (this.#options.readyTimeoutMs ?? 60000) + pieces;
    const torrent = await new Promise<WtTorrent>((resolve, reject) => {
      let settled = false;
      const finish = (fn: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn();
      };
      const timer = setTimeout(() => {
        finish(() =>
          reject(
            new Error(
              `timed out after ${timeoutMs}ms waiting for torrent metadata (${this.key})`,
            ),
          ),
        );
      }, timeoutMs);

      let added: WtTorrent;
      try {
        added = (client as WtClient).add(this.#torrentId, addOptions, (t) =>
          finish(() => resolve(t)),
        );
      } catch (error) {
        finish(() => reject(error as Error));
        return;
      }
      added.once("error", (error: unknown) => {
        // A shared client reports a duplicate by destroying the torrent it just
        // built and then invoking the callback with the one it already holds,
        // so this particular error is not fatal — the callback still resolves
        // us. Record that the torrent is not ours, so destroy() does not tear
        // it out from under whoever added it first.
        if (/duplicate torrent/i.test((error as Error)?.message ?? "")) {
          this.#ownsTorrent = false;
          return;
        }
        finish(() => reject(error as Error));
      });
    });

    this.#torrent = torrent;
    const file = pickFile(torrent, this.#options.filePath);
    this.#file = file;

    // The resume file is keyed by the source identifier, which for a .torrent
    // path says nothing about the torrent's identity. If it turns out to
    // describe a different torrent, the bitfield we just handed over is
    // meaningless, so re-add without it rather than trust unverified pieces.
    if (resume && resume.infoHash !== torrent.infoHash) {
      await this.#discardResume();
      this.#torrent = undefined;
      this.#file = undefined;
      torrent.destroy({ destroyStore: false });
      throw new Error(
        `resume data for ${this.key} describes torrent ${resume.infoHash}, not ${torrent.infoHash}; discarded, retry to verify from disk`,
      );
    }

    this.#startResumeTimer();

    return {
      infoHash: torrent.infoHash,
      pieceLength: torrent.pieceLength,
      numPieces: torrent.pieces.length,
      fileLength: file.length,
      fileOffset: file.offset,
      name: file.name,
    };
  }
}
