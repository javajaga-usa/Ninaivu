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

/**
 * Wait for the converted copy. Resolves to its URL, or null.
 *
 * Asked with HEAD: the answer is all that is wanted here. A GET answered with
 * the finished file brought the whole clip down once for the question and
 * again for the <video>, and on a phone away from home that is twice the
 * bytes for nothing.
 */
async function converted(url, stillWanted) {
  for (let i = 0; i < POLL_TRIES; i += 1) {
    if (!stillWanted()) return null;
    let response;
    try {
      response = await fetch(url, { method: 'HEAD', headers: { Accept: 'application/json' } });
    } catch {
      return null;
    }
    if (response.ok) return url;
    if (response.status !== 202) return null;
    await new Promise((done) => { setTimeout(done, POLL_MS); });
  }
  return null;
}

/*
 * Whether this browser has already been unable to open an original clip.
 * One iPhone clip it cannot play means none of them will play (they are all
 * HEVC), so after the first the rest go straight to the converted copy rather
 * than fetching an original only to fail on it, once for every hover. Kept
 * for the session, so a reload remembers it too.
 */
const ORIGINAL_FAILS = 'ninaivu.live-original-fails';
let originalFails = false;
try {
  originalFails = sessionStorage.getItem(ORIGINAL_FAILS) === '1';
} catch {
  // Storage refused (a private window): remembered for this page only.
}

function rememberOriginalFails() {
  originalFails = true;
  try {
    sessionStorage.setItem(ORIGINAL_FAILS, '1');
  } catch {
    // As above: this page still remembers.
  }
}

/**
 * Let a clip go: stop it, and drop its source so the browser gives back the
 * decoder and the buffered video now rather than whenever the element is
 * collected. Removing a <video> from the page only pauses it.
 */
export function releaseVideo(video) {
  if (!video) return;
  video.pause();
  video.removeAttribute('src');
  video.load();
  video.remove();
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
  const unavailable = () => video.dispatchEvent(new CustomEvent('live-unavailable'));
  const fallBack = async () => {
    fellBack = true;
    const url = await converted(convert, () => wanted() && video.isConnected);
    if (url) {
      video.src = url;
      if (video.dataset.playing === '1') video.play().catch(() => {});
    } else {
      unavailable();
    }
  };
  video.addEventListener('error', () => {
    // A clip let go of on purpose (releaseVideo) has no source to fail.
    if (!video.getAttribute('src')) return;
    // The converted copy failing as well is the end of it: said once, so the
    // Live button stops offering what will not come, rather than spinning.
    if (fellBack || !convert) {
      unavailable();
      return;
    }
    // A clip this browser cannot open says every other one will be the same;
    // a dropped connection says nothing about the next.
    const code = video.error?.code;
    if (code === 3 || code === 4) rememberOriginalFails();
    fallBack();
  });
  if (convert && originalFails) {
    // Straight to the copy. After the caller has put the video on the page,
    // since a conversion is only waited for while it is there.
    setTimeout(fallBack, 0);
  } else {
    video.src = src;
  }
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
