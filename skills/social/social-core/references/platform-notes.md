# Platform notes

Per-platform UI behaviour, learned in use on real accounts. Keep this file updated as
you learn: these UIs change without warning, and these notes are the difference between
a smooth run and rediscovering the same quirk monthly. The examples below come from
accounts whose UI language was Indonesian; the labels will differ in yours, the shapes
will not. Confirm any layout with a `browser_snapshot` before trusting it.

## Threads: the primary channel

Genuine keyword search with a recent sort, text-first posts, so the intent is in the
post itself. The site is `threads.com` (`threads.net` redirects).

### Search by URL, do not click the tabs

```
Recent:  https://www.threads.com/search?q=<url-encoded>&serp_type=default&filter=recent
Top:     https://www.threads.com/search?q=<url-encoded>&serp_type=default
Author:  https://www.threads.com/search?q=<term>&from_author=<handle>&serp_type=default
```

Recent is mostly sellers reposting ads with a few fresh buyers; Top has better buyer
posts but most are too old for the freshness rule. Do both. The author-scoped search
pins one post with its permalink and no scrolling, and doubles as the cheapest way to
audit what the brand's own account has replied to (`from_author=<your handle>`).

### Extraction

`browser_snapshot` gives the rendered text with element refs; `browser_read` gives the
page text. Permalinks look like `/@handle/post/<POST_ID>`. Long lists are virtualised:
posts unload as you scroll past them, so capture in passes and do not fight a blanked
column; pin a post with the author-scoped search instead.

### Replying: the composer trap

Clicking the inline reply box sometimes opens a **new thread** composer on top of the
reply composer. Typing then goes nowhere visible, and submitting would publish a
brand-new public post from the account, which these skills must never do. So, every time:

1. `browser_click` the reply box (reads like "Reply to <handle>…").
2. `browser_type` the reply.
3. **`browser_screenshot` and read it.** If a modal titled like "New thread" is showing,
   click its Cancel. The composer underneath, headed "Reply" and quoting the target
   post, is the right one: click into it, retype, screenshot again.
4. Submit (the arrow on the inline box, or Send in the modal composer).

A toast like "Posted" appears and the post's reply count increments. Check the count;
the toast alone can appear while the reply is filtered. If the count does not move,
treat it as a suppression signal, do not retry, and run Social Health.

Other traps seen: a click on the reply box navigating to a different post (check the
URL before typing); the viewport resizing between screenshots (re-screenshot right
before any click that matters).

### Direct messages: the real inbox

```
Inbox:   https://www.threads.com/messages/
Thread:  https://www.threads.com/messages/t/<thread_id>
```

Two tabs: inbox and requests. **Inbound from strangers lands in requests and must be
accepted before a reply is possible.** Accepting is the account owner's call (it also
exposes activity status); surface it, do not click it. Composer at the bottom, send is
the paper-plane button. Timestamps in the thread list refer to the last message, which
may be outbound; open the thread before reading "2h" as "they messaged 2 hours ago".

### Comments on the brand's own posts

The activity view defaults to "All", which is mostly suggested threads from other
accounts, noise that buries the real items. Filter it to Replies:
`https://www.threads.com/activity/replies`. A heart badge on an avatar means a like of
*our* post, not something they wrote; "your reply was seen N times" entries are reach
notifications, not comments.

## TikTok

### Loading: drive the UI, do not navigate straight to a search URL

Going directly to `tiktok.com/search?q=…` reliably stalls (the tab loads but scripts
never run). Instead: `browser_open` `https://www.tiktok.com/foryou`, wait about ten
seconds (grey skeleton placeholders on the first screenshot are normal), click the
search box, `browser_type` the query, `browser_press` Enter. Business-suite URLs load
directly. **This stall is a tooling quirk, not a platform block: never set a cooldown
for it.**

### Prospecting here is poor, for structural reasons

Caption search returns sellers only, and comment intent sits on videos months old with
no upload-date filter in the web UI. Do not spend prospecting time here. TikTok's value
is the inbox and comments on the brand's own videos.

### Comments on the brand's own videos

The business suite's comment page lists videos with "N new comments" but never renders
the comment text, and clicking a video clears its badge. Use the profile's activity
view filtered to comments to find them (it lists answered and unanswered alike), open
the video (`https://www.tiktok.com/@<handle>/video/<video_id>`, wait ~15 s), read the
comments tab, expand "view N replies" before answering, click "Reply" under the comment,
then **click into the new inline reply input before typing**: typing straight after
"Reply" goes nowhere, silently. Confirm the count incremented.

### Direct messages live in the business suite

```
Messages:  https://www.tiktok.com/business-suite/messages
Comments:  https://www.tiktok.com/business-suite/comments
```

**The unread badge lies.** Most "unread" items are share-to-DM noise, weeks old, while
real inquiries sit in the read list because someone replied once and stopped. Sort by
recency and read the last message.

## Instagram

Keyword search barely works: hashtag pages and comments on recent Reels only, and a poor
yield. DM volume is low, but always check the **Requests** folder; cold inbound from
non-followers lands there unseen for weeks. Threads DMs route through Instagram's inbox
for linked accounts; check both.

## General

- Confirm the logged-in account on screen at the start of every run.
- Never click delete, block, report, or any control that opens a native dialog; a modal
  freezes automation and the session cannot recover on its own.
- If a page will not load or an element will not respond after two or three attempts,
  stop and report. Do not keep hammering it, and do not mistake it for a block.
