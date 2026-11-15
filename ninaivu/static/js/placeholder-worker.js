/**
 * Fetches a block of blurred previews and draws them, off the page's thread
 * (placeholder.js asks; placeholder-codec.js draws). A block is a few hundred
 * tiles, and drawing that many on the page during a fling cost frames.
 */
import { readAnswer } from './placeholder-codec.js';

self.addEventListener('message', async (event) => {
  const { seq, ids } = event.data || {};
  try {
    const answer = await fetch(`/api/placeholders?ids=${ids.join(',')}`, {
      credentials: 'same-origin',
      priority: 'high',
    });
    if (!answer.ok) throw new Error(String(answer.status));
    const urls = readAnswer(ids, await answer.arrayBuffer());
    self.postMessage({ seq, urls });
  } catch (error) {
    self.postMessage({ seq, error: String(error && error.message || error) });
  }
});
