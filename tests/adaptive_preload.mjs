import assert from 'node:assert/strict';
import {
  AdaptivePreloader,
  DONE_LIMIT,
  connectionProfile,
  interleaveDirectional,
  predictionMs,
} from '../ninaivu/static/js/preload-policy.js';

function check(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    console.error(`not ok - ${name}`);
    throw error;
  }
}

check('desktop gets a deep but bounded settle preload', () => {
  const p = connectionProfile({ connection: {}, deviceMemory: 8, touch: false });
  assert.equal(p.aheadScreens, 8);
  assert.equal(p.behindScreens, 3);
  assert.equal(p.concurrency, 3);
});

check('touch devices spend less memory and bandwidth', () => {
  const p = connectionProfile({ connection: {}, deviceMemory: 8, touch: true });
  assert.equal(p.aheadScreens, 5);
  assert.equal(p.behindScreens, 2);
  assert.equal(p.concurrency, 2);
});

check('data saver and 2G collapse speculative preload', () => {
  for (const connection of [{ saveData: true }, { effectiveType: '2g' }, { effectiveType: 'slow-2g' }]) {
    const p = connectionProfile({ connection, deviceMemory: 8, touch: false });
    assert.equal(p.lean, true);
    assert.equal(p.aheadScreens, 1.5);
    assert.equal(p.behindScreens, 0);
    assert.equal(p.concurrency, 1);
  }
});

check('low-memory devices reduce the preload budget', () => {
  const p = connectionProfile({ connection: {}, deviceMemory: 2, touch: false });
  assert.ok(p.aheadScreens < 8);
  assert.ok(p.behindScreens < 3);
  assert.ok(p.concurrency <= 2);
});

check('prediction shrinks as scrolling accelerates and stops on a fling', () => {
  assert.equal(predictionMs(0.2), 1400);
  assert.equal(predictionMs(1), 900);
  assert.equal(predictionMs(2), 600);
  assert.equal(predictionMs(4), 450);
  assert.equal(predictionMs(5), 0);
});

check('directional queue keeps the 2 ahead : 1 behind bias', () => {
  const result = interleaveDirectional(['a1', 'a2', 'a3', 'a4'], ['b1', 'b2']);
  assert.deepEqual(result, ['a1', 'a2', 'b1', 'a3', 'a4', 'b2']);
});

check('the finished list stays bounded however recent its pictures are', () => {
  const preloader = new AdaptivePreloader({ profile: () => ({}) });
  const now = performance.now();
  for (let i = 0; i < DONE_LIMIT + 500; i += 1) preloader.done.set(`/api/thumb/${i}`, now);
  preloader.clearOldDone();
  assert.ok(preloader.done.size <= 3500, `kept ${preloader.done.size}`);
  // The newest are the ones kept.
  assert.ok(preloader.done.has(`/api/thumb/${DONE_LIMIT + 499}`));
  assert.ok(!preloader.done.has('/api/thumb/0'));
});
