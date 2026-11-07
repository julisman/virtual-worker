// Virtual Worker Companion — service worker.
//
// Tabs the Virtual Worker control browser opens announce themselves: the
// backend sets window.name = "vw-agent" on each tab it creates, the content
// script sees that marker and sends an "vw-agent-tab" message, and this worker
// moves the tab into a "Virtual Worker" tab group in its window, the same UX as
// Claude in Chrome's task groups. chrome.tabGroups is extension-only, which is
// why this cannot be done over CDP from the backend.
//
// Rules: never touch a tab that is already in any group (ours or the user's),
// never close or navigate anything, and never talk to a server.

const GROUP_TITLE = "Virtual Worker";
const GROUP_COLOR = "blue";

async function groupAgentTab(tab) {
  if (!chrome.tabGroups || !tab || tab.id == null) return;
  if (tab.groupId != null && tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE) {
    return; // already grouped
  }
  const existing = await chrome.tabGroups.query({ windowId: tab.windowId, title: GROUP_TITLE });
  if (existing.length) {
    await chrome.tabs.group({ tabIds: tab.id, groupId: existing[0].id });
  } else {
    const groupId = await chrome.tabs.group({ tabIds: tab.id });
    await chrome.tabGroups.update(groupId, { title: GROUP_TITLE, color: GROUP_COLOR });
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "vw-agent-tab" && sender.tab) {
    groupAgentTab(sender.tab).catch((e) =>
      console.warn("Virtual Worker Companion: tab grouping failed", e && e.message));
  }
});
