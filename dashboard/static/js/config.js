/* Where the ComNote API (the FastAPI backend) lives.
 *
 * Served by the backend itself (python app.py)  → same origin, leave it empty.
 * Deployed to Firebase Hosting (com-note.web.app) → the backend's public URL.
 *
 * Only API_URL needs changing before `firebase deploy`.
 *
 * Testing against a backend on your own machine: open
 *   https://com-note.web.app/?api=http://localhost:5005
 * once. Your browser remembers it; nobody else is affected. Undo with ?api=default.
 * Only localhost addresses are accepted, so a link can never point an officer's
 * login at someone else's server.
 */
(function () {
  var API_URL = "https://drinking-lions-observer-vernon.trycloudflare.com";   // ← your backend, no trailing slash
  var WHATSAPP_NUMBER = "2348134232353";                         // ← the ComNote WhatsApp line

  var KEY = "comnote-api-override";
  var LOCAL = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
  var override = null;
  try {
    var asked = new URLSearchParams(location.search).get("api");
    if (asked === "default") localStorage.removeItem(KEY);
    else if (asked && LOCAL.test(asked.replace(/\/+$/, ""))) localStorage.setItem(KEY, asked.replace(/\/+$/, ""));
    override = localStorage.getItem(KEY);
  } catch (e) { /* storage blocked — no override */ }

  var onFirebase = /\.web\.app$|\.firebaseapp\.com$/.test(location.hostname);
  window.COMNOTE_API = override || (onFirebase ? API_URL.replace(/\/+$/, "") : "");
  window.COMNOTE_API_IS_LOCAL_TEST = !!override;
  window.COMNOTE_WHATSAPP = WHATSAPP_NUMBER;
})();
