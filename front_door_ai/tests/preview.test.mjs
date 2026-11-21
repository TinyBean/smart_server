import assert from 'node:assert/strict';
import test from 'node:test';
import '../app/static/preview.js';

function setup() {
  let now = 0, nextId = 0;
  const timers = new Map(), fetches = [], revoked = [], created = [];
  const image = {dataset: {}, style: {}, src: '', removeAttribute() { this.src = ''; }};
  const env = {
    AbortController,
    setTimeout(fn, delay) { const id = ++nextId; timers.set(id, {fn, at: now + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      fetches.push({url, options, resolve, reject});
      return promise;
    },
    URL: {
      createObjectURL() { const url = `blob:${created.length}`; created.push(url); return url; },
      revokeObjectURL(url) { revoked.push(url); },
    },
  };
  const preview = new globalThis.FramePreview(image, {streamUrl: '/ingress/api/stream',
    snapshotUrl: '/ingress/api/snapshot', env});
  function tick(ms) {
    const end = now + ms;
    while (true) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].fn();
    }
    now = end;
  }
  async function respond(index, ok = true) {
    fetches[index].resolve({ok, blob: async () => new Blob(['jpeg'], {type: 'image/jpeg'})});
    for (let i = 0; i < 5; i++) await Promise.resolve();
  }
  return {preview, image, timers, fetches, revoked, created, tick, respond};
}

test('valid MJPEG stays native and repeated status refresh does not reconnect', () => {
  const s = setup(); s.preview.start(); const load = s.image.onload;
  s.preview.start(); assert.equal(s.image.onload, load);
  load(); s.tick(10000);
  assert.equal(s.image.style.display, 'block');
  assert.equal(s.image.src, '/ingress/api/stream');
  assert.equal(s.fetches.length, 0);
  s.preview.stop(); assert.equal(s.image.src, '');
});

test('undecodable MJPEG falls back after deadline; snapshots never overlap', async () => {
  const s = setup(); s.preview.start(); s.tick(2000);
  assert.equal(s.image.dataset.previewMode, 'snapshot');
  assert.equal(s.fetches[0].url, '/ingress/api/snapshot');
  assert.equal(s.fetches[0].options.cache, 'no-store');
  s.tick(1000); assert.equal(s.fetches.length, 1);
  await s.respond(0); s.image.onload();
  assert.equal(s.image.style.display, 'block');
  const first = s.image.src;
  s.tick(199); assert.equal(s.fetches.length, 1);
  s.tick(1); assert.equal(s.fetches.length, 2);
  assert.equal(s.image.src, first);
  await s.respond(1); s.image.onload();
  assert.notEqual(s.image.src, first);
  assert.deepEqual(s.revoked, [first]);
  s.preview.stop(); assert.deepEqual(s.revoked, s.created);
  assert.equal(s.timers.size, 0);
});

test('MJPEG error falls back immediately and ignores queued stream callbacks', async () => {
  const s = setup(); s.preview.start(); const oldLoad = s.image.onload;
  s.image.onerror(); oldLoad();
  assert.notEqual(s.image.style.display, 'block');
  assert.equal(s.fetches.length, 1);
  await s.respond(0); s.image.onload();
  assert.equal(s.image.style.display, 'block');
  s.preview.stop();
});

test('stopping aborts a pending fetch and ignores its late completion', async () => {
  const s = setup(); s.preview.start(); s.image.onerror();
  s.preview.stop(); assert.equal(s.fetches[0].options.signal.aborted, true);
  await s.respond(0); s.tick(10000);
  assert.equal(s.fetches.length, 1);
  assert.equal(s.created.length, 0);
  assert.equal(s.image.src, '');
  assert.equal(s.image.style.display, 'none');
  s.preview.start(); assert.equal(s.image.src, '/ingress/api/stream');
  s.preview.stop();
});

test('stopping during image decode releases blobs and ignores late load', async () => {
  const s = setup(); s.preview.start(); s.image.onerror();
  await s.respond(0); const load = s.image.onload;
  s.preview.stop(); load(); s.tick(10000);
  assert.deepEqual(s.revoked, s.created);
  assert.equal(s.image.style.display, 'none');
  assert.equal(s.timers.size, 0);
  assert.equal(s.fetches.length, 1);
});

test('timeout aborts stalled requests and retries at a bounded rate', async () => {
  const s = setup(); s.preview.start(); s.image.onerror();
  s.tick(5000); assert.equal(s.fetches[0].options.signal.aborted, true);
  s.tick(999); assert.equal(s.fetches.length, 1);
  s.tick(1); assert.equal(s.fetches.length, 2);
  await s.respond(0); assert.equal(s.created.length, 0);
  await s.respond(1, false);
  s.tick(999); assert.equal(s.fetches.length, 2);
  s.tick(1); assert.equal(s.fetches.length, 3);
  await s.respond(2); s.image.onload();
  assert.equal(s.image.style.display, 'block');
  s.preview.stop();
});

test('bad JPEG retains the last good frame and releases failed data', async () => {
  const s = setup(); s.preview.start(); s.image.onerror();
  await s.respond(0); s.image.onload(); const first = s.image.src;
  s.tick(200); await s.respond(1); const failed = s.image.src;
  s.image.onerror(); assert.equal(s.image.src, first);
  assert.deepEqual(s.revoked, [failed]);
  s.tick(1000); assert.equal(s.fetches.length, 3);
  s.preview.stop(); assert.equal(s.timers.size, 0);
});
