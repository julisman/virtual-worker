---
name: Social Comments
description: Answer comments left on the brand's OWN posts and videos on TikTok, Threads and Instagram — questions from people who came to the brand. Public, so never links; uncapped, no dedupe, no freshness rule. Escalates public complaints as urgent.
when_to_use: When asked to check comments, answer comments, or clear the comment backlog on the brand's own content. Not for other people's posts (Social Prospect and Social Engage) and not for DMs.
version: 1.0.0
requires_skills: [social-core]
---

# Social Comments

Answer people who commented on **our own content**. They came to us and asked in public.

A comment on our own post is a hybrid: inbound like a DM, public like a reply.

| | |
|---|---|
| **Uncapped** | They asked first; answering inbound is not a spam signal. No daily budget, no pacing. |
| **No dedupe** | Do not run `seen`. Refusing to answer a customer because another brand replied to them weeks ago would be absurd. |
| **No freshness rule** | A price question under our own video deserves an answer whether it is a week or six months old. |
| **NO LINKS** | The one rule inherited from Social Engage, and it is absolute. The WhatsApp link lives in the bio. |

That last row is why this is a separate skill from Social DM: DMs allow the link, public
comments never do. Log with `kind: "comment_reply"`, not `public_reply`, so comments do
not eat the cold-outreach budget.

## Preflight

1. Ensure `social.py` is in the working folder (see Social Core).
2. `shell("python3 social.py guard <brand> <platform>")`. Exit 1: stop this account.
3. Read `brands/<brand>/BRAND.md`; answers come from there and nowhere else.

## Where the comments are

TikTok's business suite aggregates comments per video, the easiest of the three. Threads
has a replies view under activity; Instagram shows comments in activity and inline on each
post. Exact paths and traps are in Social Core's platform notes; confirm the layout with a
`browser_snapshot` before trusting it, and record what you learn.

Comment lists show answered and unanswered alike; expand the replies before answering or
you will reply twice.

## Classify each comment

- **A question** (price, availability, requirements, delivery, models): the main path. Answer it.
- **A compliment or a tag**: a short warm reply is fine and cheap; never pitch here.
- **Spam, bots, promo** (competitors advertising under our video): leave it. Do not argue.
- **Sensitive**: complaints, an existing order gone wrong, damage, money, accidents,
  anything legal. A public complaint is more urgent than a private one: every future
  viewer sees it and sees whether we answered. Append to `reports/escalations.md` marked
  **URGENT, public**, with the comment text and the post link, and log it with
  `kind: "comment_escalate"`.

## Answering

Same voice as everywhere else, per `BRAND.md`; match their language.

1. **Answer the actual question in full** from `BRAND.md`. The two most common are
   usually the price list and a requirements question; both belong written out in the kit
   so they can be answered rather than deflected. One good public answer serves everyone
   who had the same question and never asked.
2. **Then a soft invite**: "happy to help in DM" or "details in the bio".
3. **No link, no phone number.** Bio only.

Screenshot before every submit, as everywhere. Record every reply:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"comment_reply\",\"author\":\"@…\",\"post_url\":\"…\",\"topic\":\"pricelist\",\"text\":\"<what you sent>\"}'")`

## Finish

Log the run even if there was nothing to answer:
`shell("python3 social.py log --json '{\"brand\":\"<slug>\",\"platform\":\"<platform>\",\"kind\":\"run\",\"skill\":\"social-comments\",\"note\":\"comments answered\"}'")`

Report: comments answered per platform, anything escalated, and any question asked more
than once; repeats belong in `BRAND.md` under common questions.
