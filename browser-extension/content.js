// Virtual Worker Companion — content script.
//
// The control browser marks every tab it creates with window.name = "vw-agent"
// (set once; window.name survives navigation within the tab). On every page
// load in such a tab, tell the service worker so it can group the tab. Nothing
// else is read from the page and nothing is sent anywhere but the extension.

(() => {
  "use strict";
  if (window !== window.top || window.name !== "vw-agent") return;
  try {
    chrome.runtime.sendMessage({ type: "vw-agent-tab" });
  } catch {
    /* extension reloaded or context gone: never break the page */
  }
})();
