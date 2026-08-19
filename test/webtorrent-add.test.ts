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
  it("never claims to hold data it does not have", async () => {
    // `skipVerify` reads like "do not waste time checking an empty store" and
    // means the opposite: WebTorrent's own seed() sets it to declare the data
    // complete. On a store holding nothing the torrent then claims every
    // piece, never downloads one, and the first read fails inside the store
    // with "Index 0 does not exist" — which is what a browser did with it.
    const { client, seen } = recordingClient();
    const engine = new WebTorrentEngine(metainfo(4), {
      client,
      readyTimeoutMs: 5,
    });
    await engine.ready().catch(() => {});

    assert.equal(seen.length, 1);
    assert.equal(seen[0].options.skipVerify, undefined);
    assert.equal(seen[0].options.deselect, true);
  });

  it("says the same with a store on disk", async () => {
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
