/**
 * Live Photos: the short clip an iPhone takes with a photograph.
 *
 * The clip is played from the original when the browser can open it, which
 * Safari always can. Chrome, Firefox and Android cannot open an iPhone's HEVC
 * clip, so when the original fails the same clip is asked for converted
 * (`?convert=1`): the server answers 202 while it makes the copy, a moment
 * for three seconds of video, and the file once it is there.
 */

const POLL_MS = 600;
const POLL_TRIES = 40;

/** Clip URLs, and the converted ones, for a still's id. */
export function liveUrls(id) {
  const src = `/api/live-video/${id}`;
  return { src, convert: `${src}?convert=1` };
}

/** Wait for the converted copy. Resolves to its URL, or null. */
async function converted(url, stillWanted) {
  for (let i = 0; i < POLL_TRIES; i += 1) {
    if (!stillWanted()) return null;
    let response;
    try {
      response = await fetch(url, { headers: { Accept: 'application/json' } });
    } catch {
      return null;
    }
    if (response.ok) return url;
    if (response.status !== 202) return null;
    await new Promise((done) => { setTimeout(done, POLL_MS); });
  }
  return null;
}

/**
 * A <video> for a live photo's clip that finds a source the browser plays.
 *
 * `wanted()` says whether it is still wanted (the tile is still hovered, the
 * viewer still on this photograph); a conversion stops being waited for the
 * moment it is not.
 */
export function liveVideo({ src, convert }, { wanted = () => true, className = '' } = {}) {
  const video = document.createElement('video');
  if (className) video.className = className;
  video.muted = true;
  video.playsInline = true;
  video.preload = 'auto';
  video.disablePictureInPicture = true;
  video.setAttribute('aria-hidden', 'true');
  let fellBack = false;
  video.addEventListener('error', async () => {
    if (fellBack || !convert) return;
    fellBack = true;
    const url = await converted(convert, () => wanted() && video.isConnected);
    if (url) {
      video.src = url;
      if (video.dataset.playing === '1') video.play().catch(() => {});
    } else {
      video.dispatchEvent(new CustomEvent('live-unavailable'));
    }
  });
  video.src = src;
  return video;
}

/** Whether to start a clip by itself, without anyone asking. */
export function mayAutoplay() {
  return !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export const LIVE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true">'
  + '<circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none"/>'
  + '<circle cx="12" cy="12" r="5.6"/>'
  + '<circle cx="12" cy="12" r="9" stroke-dasharray="1.6 2.2"/></svg>';
