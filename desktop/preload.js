// Minimal preload — kept for a stable, sandboxed bridge if the UI ever needs to
// query the native shell (app version, update status). Nothing is exposed yet.
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("vesDesktop", {
  // package.json is packaged at the asar root next to this file, so this is
  // the installed app version (npm_package_version only exists under npm dev).
  version: require("./package.json").version || null,
  platform: process.platform,
});
