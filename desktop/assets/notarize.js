// electron-builder afterSign hook — notarizes the signed .app with Apple.
//
// Runs ONLY when the Apple credentials are present in the environment, so an
// unsigned local build just skips it. Set these to enable notarization:
//   APPLE_ID                     your Apple Developer account email
//   APPLE_APP_SPECIFIC_PASSWORD  an app-specific password (appleid.apple.com)
//   APPLE_TEAM_ID                your 10-char Team ID
// (Signing itself needs a "Developer ID Application" cert in the login keychain,
//  or CSC_LINK / CSC_KEY_PASSWORD pointing at a .p12.)

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== "darwin") return;

  const { APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID } = process.env;
  if (!APPLE_ID || !APPLE_APP_SPECIFIC_PASSWORD || !APPLE_TEAM_ID) {
    console.log("[notarize] Apple credentials not set — skipping (unsigned build).");
    return;
  }

  let notarize;
  try {
    ({ notarize } = require("@electron/notarize"));
  } catch {
    console.log("[notarize] @electron/notarize not installed — skipping.");
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  console.log(`[notarize] notarizing ${appName}.app …`);
  await notarize({
    appPath: `${appOutDir}/${appName}.app`,
    appleId: APPLE_ID,
    appleIdPassword: APPLE_APP_SPECIFIC_PASSWORD,
    teamId: APPLE_TEAM_ID,
  });
  console.log("[notarize] done.");
};
