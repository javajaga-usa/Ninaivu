// Phone backup on a slow, patchy link (static/js/phone-backup.js): pieces
// sized to the link, a stalled piece given up on and sent again, a piece whose
// answer was lost carried on from the server's count, one file turned away
// without stopping the rest, and the names of unfinished files remembered.
import assert from 'node:assert/strict';

const kept = new Map();
globalThis.localStorage = {
  getItem: (k) => (kept.has(k) ? kept.get(k) : null),
  setItem: (k, v) => kept.set(k, String(v)),
  removeItem: (k) => kept.delete(k),
};
function node() {
  return {
    textContent: '', value: '', hidden: false, disabled: false, style: {}, className: '',
    addEventListener() {}, prepend() {}, append() {}, replaceChildren() {}, click() {},
  };
}
const nodes = new Map();
globalThis.document = {
  querySelector: (sel) => { if (!nodes.has(sel)) nodes.set(sel, node()); return nodes.get(sel); },
  createElement: () => node(),
  addEventListener() {},
  visibilityState: 'visible',
};

const { PhoneBackup, nextPiece, pieceTimeout, dropped, PIECE_MIN, PIECE_MAX, FINISH_MS } = await import(
  new URL('../ninaivu/static/js/phone-backup.js', import.meta.url));

// -- the sizes ------------------------------------------------------------------
const MB = 1024 * 1024;
assert.equal(nextPiece(MB, { bytes: MB, seconds: 1 }), 2 * MB, 'never more than double');
assert.equal(nextPiece(4 * MB, { bytes: 4 * MB, seconds: 40 }), 512 * 1024, 'about five seconds at the speed measured');
assert.equal(nextPiece(MB, { dropped: true }), 512 * 1024, 'halved after a drop');
assert.equal(nextPiece(PIECE_MIN, { dropped: true }), PIECE_MIN, 'never below the smallest');
assert.equal(nextPiece(PIECE_MAX, { bytes: 100 * MB, seconds: 1 }), PIECE_MAX, 'never above the largest');
assert.equal(pieceTimeout(MB, 0), 30000, 'half a minute on a link not measured yet');
assert.equal(pieceTimeout(MB, 10 * 1024), 180000, 'never more than three minutes');
assert.equal(pieceTimeout(MB, 0, 2), 120000, 'longer after drops in a row');
assert.ok(dropped(new TypeError('Failed to fetch')));
console.log('PASS: pieces are sized to the link');

// -- a server, and a link that misbehaves ---------------------------------------
const files = new Map();       // id -> {size, have, state}
let nextId = 1;
const script = [];             // what the link does to each PUT, in order
const puts = [];
function answer(status, body) {
  return { ok: status < 400, status, statusText: '', json: async () => body };
}
globalThis.fetch = async (url, options = {}) => {
  const path = new URL(url, 'http://ninaivu.test');
  if (path.pathname === '/api/phone-backup/files' && options.method === 'POST') {
    const body = JSON.parse(options.body);
    if (body.name === 'refused.mov') return answer(400, { error: 'That file type is not one of them.' });
    const id = nextId++;
    files.set(id, { size: body.size, have: 0, state: 'receiving' });
    return answer(200, { id, offset: 0, state: 'receiving' });
  }
  if (path.pathname.startsWith('/api/phone-backup/files/')) {
    const row = files.get(Number(path.pathname.split('/').pop()));
    const offset = Number(path.searchParams.get('offset'));
    const size = options.body.size;
    const what = script.shift() || 'ok';
    puts.push(size);
    if (what === 'drop') throw new TypeError('Failed to fetch');
    if (what === 'stall') {
      return new Promise((_, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
      });
    }
    if (offset !== row.have) return answer(409, { error: 'Carry on', offset: row.have });
    row.have += size;
    if (row.have === row.size) row.state = 'staged';
    if (what === 'lost') throw new TypeError('Failed to fetch');   // arrived, answer lost
    return answer(200, { id: 1, offset: row.have, state: row.state });
  }
  if (path.pathname === '/api/phone-backup/check') {
    const body = JSON.parse(options.body);
    return answer(200, { files: body.files.map(() => ({ state: 'new' })), summary: {} });
  }
  if (path.pathname === '/api/phone-backup/finish') return answer(200, { approved: 0, needs_review: true, summary: {} });
  return answer(404, {});
};

const toasts = [];
const backup = new PhoneBackup({ toast: (m) => toasts.push(m) });
// No real waiting in a test: no pause between tries, and a stall is given up on at once.
backup.waitForLink = async function wait() { this.wait = 0; return PhoneBackup.prototype.waitForLink.call(this); };
backup.timed = (ms, attempt) => PhoneBackup.prototype.timed.call(backup, 200, attempt);

function file(name, size) {
  const blob = new Blob([new Uint8Array(size)]);
  Object.defineProperty(blob, 'name', { value: name });
  Object.defineProperty(blob, 'lastModified', { value: 1700000000000 });
  return blob;
}

const video = file('IMG_0001.MOV', 5 * MB + 12345);
script.push('ok', 'drop', 'stall', 'lost');
const done = await backup.send({ file: video }, () => {});
assert.equal(done.state, 'staged', 'the whole file arrived');
assert.equal(files.get(1).have, video.size, 'every byte, once');
assert.equal(puts[0], MB, 'the first piece is small on a link not measured yet');
assert.ok(puts[2] <= puts[1] / 2 + 1 && puts[3] <= puts[1], 'smaller after a drop and a stall');
console.log('PASS: a dropped, a stalled and a lost piece are each carried on from the server\'s count');

// -- one file turned away, the rest carry on; unfinished names remembered ------------
await backup.run([file('a.jpg', 300000), file('refused.mov', 1000), file('b.jpg', 200000)]);
assert.equal(nodes.get('#backup-status').textContent.includes('Could not back up'), false,
  'a file turned away does not end the run');
assert.ok(toasts.some((t) => t.startsWith('refused.mov')), 'and is named');
const left = JSON.parse(kept.get('ninaivu.backup.unfinished') || '{"names":[]}');
assert.deepEqual(left.names, ['refused.mov'], 'only the file that did not finish is remembered');
console.log('PASS: one file turned away is marked and the rest carry on');

// Stop: the piece on its way is abandoned, the names stay for next time.
script.push('stall');
const run = backup.run([file('c.jpg', 600000)]);
await new Promise((r) => { setTimeout(r, 30); });
backup.stop();
await run;
assert.deepEqual(JSON.parse(kept.get('ninaivu.backup.unfinished')).names, ['c.jpg']);
backup.showUnfinished();
assert.match(nodes.get('#backup-status').textContent, /One file did not finish last time/);
console.log('PASS: Stop leaves the unfinished file named for next time');

// The end of a batch is where the server files it, which can take minutes:
// it gets far longer than a small answer before it is given up on.
const slow = new PhoneBackup({ toast: () => {} });
const waits = [];
slow.timed = (ms, attempt) => { waits.push([ms, attempt.toString()]); return PhoneBackup.prototype.timed.call(slow, 200, attempt); };
await slow.run([file('d.jpg', 1000)]);
const finish = waits.find(([, text]) => text.includes('/finish'));
assert.ok(finish && finish[0] >= FINISH_MS, 'the finish is given minutes, not half a minute');
assert.ok(waits.filter(([, text]) => text.includes('/check')).every(([ms]) => ms < FINISH_MS));
console.log('PASS: filing the batch is not abandoned after half a minute');
