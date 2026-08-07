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
  ready: boolean;
  destroyed: boolean;
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
    ontorrent?: (torrent: WtTorrent) => void
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

  /** How long to wait for torrent metadata. Default 60s. */
  readyTimeoutMs?: number;
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
      }
    );
  });
}

/** Pull an infohash out of a magnet URI so `getKey()` works before metadata arrives. */
function deriveKey(torrentId: unknown): string {
  if (typeof torrentId !== "string") return "torrent:unknown";
  const magnet = /xt=urn:bt[im]h:([a-z0-9]+)/i.exec(torrentId);
  if (magnet) return `torrent:${magnet[1].toLowerCase()}`;
  if (/^[a-f0-9]{40}$/i.test(torrentId)) return `torrent:${torrentId.toLowerCase()}`;
  return `torrent:${torrentId}`;
}

function pickFile(torrent: WtTorrent, filePath?: string): WtFile {
  if (torrent.files.length === 0) throw new Error("torrent contains no files");

  if (filePath) {
    const match = torrent.files.find(
      (file) => file.path === filePath || file.name === filePath
    );
    if (!match) {
      throw new Error(
        `no file "${filePath}" in torrent (has: ${torrent.files
          .map((f) => f.path)
          .join(", ")})`
      );
    }
    return match;
  }

  const byLength = [...torrent.files].sort((a, b) => b.length - a.length);
  return byLength.find((file) => file.name.endsWith(".pmtiles")) ?? byLength[0];
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
  #readyPromise?: Promise<TorrentInfo>;
  #destroyed = false;

  constructor(torrentId: unknown, options: WebTorrentEngineOptions = {}) {
    this.#torrentId = torrentId;
    this.#options = options;
    this.key = deriveKey(torrentId);
  }

  /** The underlying torrent, once metadata has arrived. For swarm/seeding stats. */
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
    options: ReadRangeOptions = {}
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
          signal
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
        `short read from torrent: got ${written} of ${length} bytes at offset ${offset}`
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
      (file.offset + offset + length - 1) / torrent.pieceLength
    );
    torrent.select(first, last, PRIORITY_VALUES[priority]);
    if (priority === "critical") torrent.critical(first, last);
  }

  async destroy(): Promise<void> {
    if (this.#destroyed) return;
    this.#destroyed = true;

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
    // torrents) alone. Keep the store so we can resume seeding later.
    if (torrent && !torrent.destroyed) torrent.destroy({ destroyStore: false });
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
      // exercise is serving a 100 GB map without holding 100 GB.
      deselect: true,
    };
    if (this.#options.path) addOptions.path = this.#options.path;
    if (this.#options.announce) addOptions.announce = this.#options.announce;

    const timeoutMs = this.#options.readyTimeoutMs ?? 60_000;
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
              `timed out after ${timeoutMs}ms waiting for torrent metadata (${this.key})`
            )
          )
        );
      }, timeoutMs);

      let added: WtTorrent;
      try {
        added = (client as WtClient).add(this.#torrentId, addOptions, (t) =>
          finish(() => resolve(t))
        );
      } catch (error) {
        finish(() => reject(error as Error));
        return;
      }
      added.once("error", (error: unknown) =>
        finish(() => reject(error as Error))
      );
    });

    this.#torrent = torrent;
    const file = pickFile(torrent, this.#options.filePath);
    this.#file = file;

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
        `(${(error as Error).message})`
    );
  }
}
