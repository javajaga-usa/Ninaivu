/**
 * Scroll bars that stay out of sight until they are wanted.
 *
 * Every scrolling box in both apps keeps its bar clear (premium.css, "Scroll
 * bars") until it scrolls: then it shows, thin and in the page's own colours,
 * and fades once the box has rested for a moment. The smaller panes show it
 * under the pointer too, so it can still be found and dragged; the gallery and
 * the page itself only while they move, since the pointer is always over them.
 *
 * One listener for the whole page, on the capture side so it hears scrolls
 * that do not bubble, and passive, so it never holds a scroll up. Marking a
 * box is a class it already has or not: nothing is written while it scrolls
 * but a timer, and the class goes once, when it rests.
 */
const REST_MS = 900;

export function hideScrollBarsUntilUsed(doc = document) {
  const resting = new WeakMap();
  doc.addEventListener('scroll', (event) => {
    const box = event.target === doc ? doc.documentElement : event.target;
    if (!box?.classList) return;
    if (!box.classList.contains('bars-on')) box.classList.add('bars-on');
    clearTimeout(resting.get(box));
    resting.set(box, setTimeout(() => box.classList.remove('bars-on'), REST_MS));
  }, { capture: true, passive: true });
}
