/**
 * A byte-budgeted LRU cache of torrent pieces.
 *
 * Pieces are big — a 16 MiB piece length is common for large archives — so an
 * unbounded map fills the heap with data the engine's own chunk store already
 * holds on disk. This keeps the hot pieces (directories, whatever tiles are
 * being requested right now) in memory and lets the rest fall back to the
 * engine.
 */
export class PieceCache {
  #maxBytes: number;
  #bytes = 0;
  // Map preserves insertion order, so the first key is always the LRU entry.
  #entries = new Map<number, Uint8Array>();

  /**
   * @param maxBytes Byte budget. Zero disables caching entirely.
   */
  constructor(maxBytes: number) {
    this.#maxBytes = Math.max(0, maxBytes);
  }

  /** Total bytes currently held. */
  get byteLength(): number {
    return this.#bytes;
  }

  /** Current byte budget. */
  get maxBytes(): number {
    return this.#maxBytes;
  }

  /** Number of pieces currently held. */
  get size(): number {
    return this.#entries.size;
  }

  /**
   * Changes the budget, evicting as needed. Used once torrent metadata arrives
   * and the real piece length is known.
   */
  resize(maxBytes: number): void {
    this.#maxBytes = Math.max(0, maxBytes);
    this.#evict();
  }

  /** Looks up a piece, marking it most recently used. */
  get(index: number): Uint8Array | undefined {
    const hit = this.#entries.get(index);
    if (hit === undefined) return undefined;
    // Re-insert to mark as most recently used.
    this.#entries.delete(index);
    this.#entries.set(index, hit);
    return hit;
  }

  /** Stores a piece, evicting the least recently used entries if over budget. */
  set(index: number, piece: Uint8Array): void {
    if (this.#maxBytes === 0) return;
    // A single piece larger than the whole budget would evict everything and
    // then itself; skip it rather than thrash.
    if (piece.byteLength > this.#maxBytes) return;

    const existing = this.#entries.get(index);
    if (existing !== undefined) {
      this.#entries.delete(index);
      this.#bytes -= existing.byteLength;
    }

    this.#entries.set(index, piece);
    this.#bytes += piece.byteLength;
    this.#evict();
  }

  /** Drops every cached piece. */
  clear(): void {
    this.#entries.clear();
    this.#bytes = 0;
  }

  /** Evicts least recently used entries until within budget. */
  #evict(): void {
    while (this.#bytes > this.#maxBytes) {
      const oldest = this.#entries.keys().next();
      if (oldest.done) break;
      const evicted = this.#entries.get(oldest.value);
      this.#entries.delete(oldest.value);
      if (evicted !== undefined) this.#bytes -= evicted.byteLength;
    }
  }
}
