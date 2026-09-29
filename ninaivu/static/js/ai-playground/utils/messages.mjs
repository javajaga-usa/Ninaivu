/**
 * Errors Sudar can say in the reader's language.
 *
 * The models and the request planner are also loaded by Node tests and the
 * image worker, where the page's i18n.js has no business being, so they throw
 * English. `failure()` keeps the English key and its values beside the
 * message; `errorText()` is where a component shows an error, and translates
 * it there. An error that came from the server is looked up as it is, and
 * stays in English if the locale does not have it.
 */

/** An Error whose message is `text` with `{name}` filled from `vars`. */
export function failure(text, vars = {}) {
  let message = text;
  for (const [name, value] of Object.entries(vars)) message = message.split(`{${name}}`).join(String(value));
  return Object.assign(new Error(message), {i18n: [text, vars]});
}

/** An error's message in the current language; `t` is i18n.t. */
export function errorText(error, t) {
  if (error?.i18n) return t(error.i18n[0], error.i18n[1]);
  return t(String(error?.message ?? error ?? ''));
}
