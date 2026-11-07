# Virtual Worker Companion (Chrome extension)

Puts the tabs your agents open into a **Virtual Worker** tab group, the way
Claude in Chrome groups its task tabs, so you can always tell your tabs from
the agent's. That is all it does: it has no popup, stores nothing, and never
contacts a server. The backend marks its tabs with `window.name = "vw-agent"`;
the extension's content script notices that marker and asks the service worker
to group the tab (`chrome.tabGroups` is extension-only, so this cannot be done
over CDP from the backend). Tabs already in a group, yours or its own, are
left alone.

## When you need it

Only in **attach mode**, where agents drive your own running Chrome. In the
default launch mode the backend starts a separate browser window, so grouping
is not needed.

## Install (sideload, no store account needed)

1. Open `chrome://extensions` and turn on **Developer mode**.
2. **Load unpacked** and pick this `browser-extension/` folder.

## Attach Virtual Worker to your Chrome

1. Start Chrome with remote debugging on a dedicated profile. Chrome 136 and
   later ignore the flag on the default profile:

   ```bash
   # macOS
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 --user-data-dir="$HOME/.virtual-worker/chrome-profile"
   # Linux
   google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.virtual-worker/chrome-profile"
   ```

2. In Virtual Worker open **Settings → Browser**, choose **Attach**, set the
   CDP URL to `http://127.0.0.1:9222`, and relaunch the control browser from
   the same card. Or set `VW_BROWSER_CDP_URL` before starting the backend.
3. Install the extension in that Chrome profile.

From then on, every tab an agent opens with `browser_open` appears in the blue
**Virtual Worker** group next to your tabs. Agents never take over one of your
tabs on their own; ask them to work in one and they can switch to it with
`browser_tab_select`.

## Updating

After pulling a new version, click **Reload** on `chrome://extensions` and
refresh any open agent tabs.
