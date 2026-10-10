/* Where the Idon-Lafia API lives.
 * Same origin (backend serves this dashboard) -> leave empty.
 * Separate host -> set the backend's URL here.
 * Local override for testing: open ?api=http://localhost:8010 once.
 */
(function () {
  var API_URL = "https://z7mjl6gp-8010.uks1.devtunnels.ms";
  var KEY = "phc-api-override";
  var LOCAL = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
  var override = null;
  try {
    var asked = new URLSearchParams(location.search).get("api");
    if (asked === "default") localStorage.removeItem(KEY);
    else if (asked && LOCAL.test(asked.replace(/\/+$/, ""))) localStorage.setItem(KEY, asked.replace(/\/+$/, ""));
    override = localStorage.getItem(KEY);
  } catch (e) {}
  window.PHC_API = override || API_URL;
})();
