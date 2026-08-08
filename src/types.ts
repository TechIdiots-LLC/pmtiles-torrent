/**
 * Fetch priority for a byte range.
 *
 * Engines map these onto whatever their BitTorrent implementation offers.
 * `critical` means a request is blocked on this right now, `high` means we will
 * almost certainly need it shortly (directories, metadata), `normal` means
 * fetch it when there is nothing better to do.
 */
export type Priority = "critical" | "high" | "normal";

/**
 * Everything the piece-mapping layer needs to know about a torrent.
 *
 * Note the split between file-relative and torrent-global coordinates: all
 * offsets passed to and from an engine are relative to the archive file, while
 * `pieceLength` and `numPieces` describe the torrent's global byte space.
 * `fileOffset` bridges the two, so a PMTiles archive packed alongside other
 * files in a multi-file torrent works without special cases.
 */
export interface TorrentInfo {
  /** Hex infohash, which doubles as the archive's ETag. */
  infoHash: string;
  /** Piece length in bytes, from the torrent's info dictionary. */
  pieceLength: number;
  /** Total number of pieces in the torrent. */
  numPieces: number;
  /** Length of the PMTiles archive itself, in bytes. */
  fileLength: number;
  /** Byte offset of the archive within the torrent's global byte space. */
  fileOffset: number;
  /** Display name of the file, for logging. */
  name?: string;
}

/** Options for a single range read. */
export interface ReadRangeOptions {
  /** Cancels the read. */
  signal?: AbortSignal;
  /** How urgently the bytes are needed. */
  priority?: Priority;
}

/**
 * The BitTorrent client abstraction.
 *
 * Deliberately tiny: read a byte range, optionally with priority and
 * cancellation. Everything PMTiles-specific — piece alignment, caching,
 * directory prefetch — lives above this line, so porting to another BitTorrent
 * implementation (or another language) only means reimplementing this.
 */
export interface TorrentEngine {
  /**
   * A stable identifier available *synchronously*, before `ready()` resolves.
   * PMTiles calls `Source.getKey()` without awaiting anything, so this cannot
   * be derived from torrent metadata.
   */
  readonly key: string;

  /** Resolves once torrent metadata is available. Must be idempotent. */
  ready(): Promise<TorrentInfo>;

  /**
   * Reads `length` bytes starting at `offset` bytes into the archive file.
   * Must resolve with exactly `length` bytes or reject.
   */
  readRange(
    offset: number,
    length: number,
    options?: ReadRangeOptions,
  ): Promise<Uint8Array>;

  /**
   * Non-blocking request to start fetching a range in the background.
   * Engines that cannot express priority may omit it.
   */
  hint?(offset: number, length: number, priority: Priority): void;

  /**
   * Withdraws a previous hint so the range stops competing for bandwidth.
   * Background hydration is only attempted when an engine provides both
   * `hint` and `unhint`.
   */
  unhint?(offset: number, length: number): void;

  /** Releases the engine's resources. */
  destroy(): void | Promise<void>;
}

/** The shape PMTiles expects back from a range read. */
export interface RangeResponse {
  data: ArrayBuffer;
  etag?: string;
  expires?: string;
  cacheControl?: string;
}
