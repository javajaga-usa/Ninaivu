/**
 * Enter does what the button beside the field does.
 *
 * Typing a username, a password, a PIN, a client secret or a folder name and
 * pressing Enter did nothing in most of Ninaivu: the sign-in screens are real
 * forms, which browsers submit on Enter, but a field sitting in a row with a
 * Save button is not a form, and nothing was listening. Every one of those
 * places wanted the same handler, written out by hand one at a time, and the
 * ones nobody had got round to were simply dead ends — the button had to be
 * found and clicked instead.
 *
 * So it is one rule, once: Enter in a text field presses the obvious button
 * beside it. *Obvious* is the point. Guessing wrong is worse than doing
 * nothing — Enter must never clear a search box or cancel a dialog — so a
 * button only counts when it is the single action in the field's own row, or
 * the one marked as that row's main action, and never when it is an icon, a
 * Cancel, a Browse or a Clear.
 */

//: Fields where Enter means "I have finished typing, get on with it".
//: `search` is deliberately absent: those search as they are typed, and their
//: one button clears them.
const FIELD_TYPES = new Set([
  "text", "password", "number", "url", "email", "tel", "date", "time",
]);

//: Buttons Enter must never press, however close to the field they sit.
const NOT_THE_ACTION = /^(cancel|close|back|browse|refresh|copy|clear|undo|dismiss|skip)\b/i;

//: How far out from the field to look. A field's own row, the group it is in,
//: and no further: a whole page's primary button is not "beside" anything.
const HOPS = 3;

//: Where to stop climbing whatever the hop count says. Past these, the next
//: thing found is another part of the page: a field whose own Save button is
//: disabled reached the *next* row's button and pressed that.
const OUTSIDE = new Set(["BODY", "HTML", "MAIN", "SECTION", "ARTICLE", "FORM",
                         "DIALOG"]);

function label(button) {
  return (button.getAttribute("aria-label") || button.textContent || "").trim();
}

function candidates(node, field) {
  return [...node.querySelectorAll("button")].filter((button) => (
    !button.disabled
    && !button.hidden
    && button.offsetParent !== null
    && (button.type || "submit") !== "reset"
    && !button.classList.contains("icon-btn")
    && !NOT_THE_ACTION.test(label(button))
    // Before the field is a heading's control or a previous row's Save; the
    // action for what has just been typed comes after it.
    && field.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING
  ));
}

/** The button Enter should press for this field, or null when unclear. */
export function obviousButton(field) {
  let node = field.parentElement;
  for (let hop = 0; node && hop < HOPS; hop += 1, node = node.parentElement) {
    if (OUTSIDE.has(node.tagName)) break;
    const buttons = candidates(node, field);
    if (!buttons.length) continue;
    const marked = buttons.find((button) => button.classList.contains("primary"))
      || buttons.find((button) => button.classList.contains("danger"));
    if (marked) return marked;
    // One action in the row is unambiguous; several with none marked is not,
    // and pressing the wrong one is the failure this exists to avoid.
    return buttons.length === 1 ? buttons[0] : null;
  }
  return null;
}

/** Wire the rule up once, for every field on the page, now and later. */
export function enterPressesTheButton(root = document) {
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing || event.defaultPrevented) return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !FIELD_TYPES.has(field.type)) return;
    // A form of its own already does this, and its own handler knows better
    // than this one which button matters.
    if (field.form) return;
    if (field.readOnly || field.disabled) return;
    const button = obviousButton(field);
    if (!button) return;
    event.preventDefault();
    button.click();
  });
}
