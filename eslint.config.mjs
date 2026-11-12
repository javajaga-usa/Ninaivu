// ESLint for the front end and the browser tests. A check in CI and nothing
// more: Ninaivu's JavaScript is served as it is written, with no build step,
// and nothing here is installed on anybody's computer.
//
//   npm install --no-save eslint@10.14.0 @eslint/js@10.0.1 globals@17.14.0
//   npx eslint ninaivu/static tests
//
// (the versions are the ones the Lint job installs, .github/workflows/tests.yml)
//
// The rules are ESLint's recommended set: mistakes, not style. Two are
// loosened where the code is written a certain way on purpose, and the reason
// is beside each.
import js from "@eslint/js";
import globals from "globals";

export default [
  // Leaflet is somebody else's file, kept as it was published.
  { ignores: ["**/leaflet.js", "node_modules/**", "installers/**", "build/**"] },
  js.configs.recommended,
  {
    files: ["ninaivu/static/**/*.{js,mjs}"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: { ...globals.browser } },
  },
  {
    // The map page draws with Leaflet, which its own <script> puts on window.
    files: ["ninaivu/static/js/pages/family/map.js"],
    languageOptions: { globals: { L: "readonly" } },
  },
  {
    files: ["ninaivu/static/sw.js", "ninaivu/static/js/**/*worker.{js,mjs}"],
    languageOptions: { globals: { ...globals.serviceworker, ...globals.worker } },
  },
  {
    rules: {
      // `catch { }` is how the pages say "this may fail and nothing follows":
      // storage in a private window, a feature the browser does not have.
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": ["error", { args: "after-used", caughtErrors: "none" }],
      // `let data = null; try { data = await ... } catch { ... }` is the
      // pages' way of reading an answer, and the rule calls its first line
      // useless.
      "no-useless-assignment": "off",
    },
  },
  {
    // It writes whole pages as strings, and `<\/script>` in them is the
    // escape that keeps such a string safe to put inside a page.
    files: ["ninaivu/static/js/creative-studio.js"],
    rules: { "no-useless-escape": "off" },
  },
  {
    // The browser tests run under Node and hand functions to the page, where
    // the page's own globals (document, editor) exist: no-undef cannot tell
    // the two apart. Helpers kept for the next test are not mistakes either.
    files: ["tests/**/*.mjs"],
    languageOptions: {
      ecmaVersion: 2024, sourceType: "module",
      globals: { ...globals.node, ...globals.browser },
    },
    rules: { "no-undef": "off", "no-unused-vars": "off" },
  },
];
