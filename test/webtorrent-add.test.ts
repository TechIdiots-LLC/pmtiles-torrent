import assert from "node:assert";
import { describe, it } from "node:test";
import { WebTorrentEngine } from "../src/engines/webtorrent.js";

/** A bencoded metainfo carrying `pieces` of the given piece count. */
function metainfo(pieces: number): Uint8Array {
  const body = `d4:infod6:pieces${pieces * 20}:${"x".repeat(pieces * 20)}ee`;
  return new TextEncoder().encode(body);
}

/**
 * A WebTorrent client that records what `add` was given and never readies.
 *
 * Never readying is the point: what these tests are about is the options the
 * torrent is added with and how long the engine is willing to wait, both of
 * which are decided before any callback fires.
 */
function recordingClient() {
  const seen: { id: unknown; options: Record<string, unknown> }[] = [];
  return {
    seen,
    client: {
      add(id: unknown, options: Record<string, unknown>) {
        seen.push({ id, options });
        return { on() {}, once() {}, removeListener() {} };
      },
      on() {},
      once() {},
      removeListener() {},
      destroy() {},
    } as never,
  };
}

describe("adding a torrent from a browser", () => {
  it("skips the verify pass when nothing is persisted", async () => {
    // There is no store to check, so a verify pass can find nothing — and it
    // is not free: WebTorrent walks every piece before `ready` fires, which on
    // a large archive is the entire metadata budget spent proving that an
    // empty store is empty.
    const { client, seen } = recordingClient();
    const engine = new WebTorrentEngine(metainfo(4), {
      client,
      readyTimeoutMs: 5,
    });
    await engine.ready().catch(() => {});

    assert.equal(seen.length, 1);
    assert.equal(seen[0].options.skipVerify, true);
    assert.equal(seen[0].options.deselect, true);
  });

  it("keeps the verify pass where there is a store to verify", async () => {
    // A node holding the archive on disk has something worth checking, and
    // skipping it would mean seeding pieces nothing had confirmed.
    const { client, seen } = recordingClient();
    const engine = new WebTorrentEngine(metainfo(4), {
      client,
      path: "/tmp/does-not-need-to-exist",
      readyTimeoutMs: 5,
    });
    await engine.ready().catch(() => {});

    assert.equal(seen[0].options.skipVerify, undefined);
  });
});

describe("how long the engine waits for metadata", () => {
  it("allows more time for a torrent with more pieces", async () => {
    // A fixed budget silently excludes the archives most worth sharing: an
    // 8.7x larger torrent was timing out at 30s where a smaller one joined.
    const small = Date.now();
    await new WebTorrentEngine(metainfo(0), {
      client: recordingClient().client,
      readyTimeoutMs: 20,
    })
      .ready()
      .catch(() => {});
    const smallMs = Date.now() - small;

    const large = Date.now();
    await new WebTorrentEngine(metainfo(300), {
      client: recordingClient().client,
      readyTimeoutMs: 20,
    })
      .ready()
      .catch(() => {});
    const largeMs = Date.now() - large;

    assert.ok(
      largeMs > smallMs,
      `expected the larger torrent to wait longer, got ${largeMs} vs ${smallMs}`,
    );
  });

  it("takes a magnet as it finds it, having no piece count to read", async () => {
    // A magnet carries an infohash and nothing about size, so there is nothing
    // to scale by and the base budget stands.
    const { client, seen } = recordingClient();
    const engine = new WebTorrentEngine("magnet:?xt=urn:btih:" + "a".repeat(40), {
      client,
      readyTimeoutMs: 5,
    });
    await engine.ready().catch(() => {});
    assert.equal(seen.length, 1);
  });
});
