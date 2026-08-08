import type {
  Priority,
  ReadRangeOptions,
  TorrentEngine,
  TorrentInfo,
} from "../src/types.js";

function abortError(): Error {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

export interface FakeEngineOptions {
  pieceLength: number;
  /** Bytes preceding the archive in the torrent, i.e. a multi-file torrent. */
  fileOffset?: number;
  /** Trailing bytes after the archive, for the same reason. */
  trailingBytes?: number;
  /** Delay before a read resolves. Ignored when `manual` is set. */
  delayMs?: number;
  /** Hold every read open until `flush()` is called. */
  manual?: boolean;
  infoHash?: string;
}

export interface RecordedRead {
  offset: number;
  length: number;
  priority?: Priority;
}

export interface RecordedHint {
  offset: number;
  length: number;
  priority: Priority;
}

/**
 * An in-memory {@link TorrentEngine} over a byte array, with controllable
 * timing so the source's concurrency and cancellation behaviour is testable
 * without a swarm.
 */
export class FakeEngine implements TorrentEngine {
  readonly key = "torrent:fake";

  reads: RecordedRead[] = [];
  hints: RecordedHint[] = [];
  unhints: { offset: number; length: number }[] = [];
  aborted = 0;
  destroyed = false;

  #data: Uint8Array;
  #info: TorrentInfo;
  #options: FakeEngineOptions;
  #waiters: Array<() => void> = [];

  constructor(data: Uint8Array, options: FakeEngineOptions) {
    this.#data = data;
    this.#options = options;
    const fileOffset = options.fileOffset ?? 0;
    const total = fileOffset + data.byteLength + (options.trailingBytes ?? 0);
    this.#info = {
      infoHash: options.infoHash ?? "a".repeat(40),
      pieceLength: options.pieceLength,
      numPieces: Math.ceil(total / options.pieceLength),
      fileLength: data.byteLength,
      fileOffset,
      name: "fake.pmtiles",
    };
  }

  /** How many reads are currently blocked. */
  get pendingReads(): number {
    return this.#waiters.length;
  }

  async ready(): Promise<TorrentInfo> {
    return this.#info;
  }

  async readRange(
    offset: number,
    length: number,
    options: ReadRangeOptions = {},
  ): Promise<Uint8Array> {
    this.reads.push({ offset, length, priority: options.priority });
    await this.#gate(options.signal);
    if (offset < 0 || offset + length > this.#info.fileLength) {
      throw new RangeError(
        `fake engine read out of bounds: ${offset}+${length} of ${this.#info.fileLength}`,
      );
    }
    return this.#data.slice(offset, offset + length);
  }

  hint(offset: number, length: number, priority: Priority): void {
    this.hints.push({ offset, length, priority });
  }

  unhint(offset: number, length: number): void {
    this.unhints.push({ offset, length });
  }

  destroy(): void {
    this.destroyed = true;
    this.flush();
  }

  /** Releases every read currently waiting. */
  flush(): void {
    const waiting = [...this.#waiters];
    this.#waiters.length = 0;
    for (const release of waiting) release();
  }

  /** Blocks until released, either by the timer or by `flush()`. */
  #gate(signal?: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (signal?.aborted) {
        this.aborted++;
        reject(abortError());
        return;
      }

      let timer: ReturnType<typeof setTimeout> | undefined;
      const cleanup = () => {
        if (timer !== undefined) clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        const index = this.#waiters.indexOf(release);
        if (index >= 0) this.#waiters.splice(index, 1);
      };
      const release = () => {
        cleanup();
        resolve();
      };
      const onAbort = () => {
        this.aborted++;
        cleanup();
        reject(abortError());
      };

      signal?.addEventListener("abort", onAbort, { once: true });
      if (this.#options.manual) {
        this.#waiters.push(release);
      } else {
        timer = setTimeout(release, this.#options.delayMs ?? 0);
      }
    });
  }
}

/** Deterministic filler so assembled ranges can be checked byte for byte. */
export function ramp(length: number): Uint8Array {
  const out = new Uint8Array(length);
  for (let i = 0; i < length; i++) out[i] = i % 251;
  return out;
}
