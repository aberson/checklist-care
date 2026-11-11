# Proposal contract (checklist-ingest)

The judging rules and the file format of a `checklist-ingest` proposal. The core,
`skills/checklist-ingest/core.md`, says when to apply them.

Judge against the values `uv run checklist-care context` printed for this run: the `limits`, the
bucket `rubric`s, the list `cue`s and the stored `tips`. The operator edits rubrics and cues in the
app, so the printed text wins over anything you remember or anything written here.

Every example in this file is self-written. None comes from a video or text the project is
evaluated on.

## Tip fields

Every tip carries every field below; write `null` (or `[]`) where the rule says so. Text fields
are one line: no line breaks, tabs or control characters.

| Field | Rule |
|---|---|
| `name` | Catchy name, at most `NAME_MAX` characters. See **Naming and polarity** |
| `action` | What to do, as one imperative line that states the healthy behavior, at most `ACTION_MAX` characters |
| `reason` | One plain-language sentence, never empty, at most `REASON_MAX` characters. No statistics, percentages or study claims you cannot verify |
| `bucket` | `1`, `2`, `3` or `4`. See **Buckets** |
| `audience` | `"kids+"` or `"adults"`. See **Audience** |
| `list_ids` | Ids from `context.lists`. Always `[]` outside bucket 1. See **Placement** |
| `start_s` | Whole seconds into the video: the chapter's `start_s` for a chaptered video, else the second the advice starts. Below `duration_s`. `null` for an article or a text |
| `locator` | In an investigation: the section heading the tip belongs to, copied exactly from `context.source.section_headings`. Otherwise the original chapter title when you renamed it, else `null` |
| `evidence_url` | In an investigation: the `http(s)` URL of the page the tip came from; in a degraded one, the article URL (the packet's `url`). Otherwise `null` |
| `duplicate_of` | The id of the stored tip this one repeats, else `null`. Only in bucket 4. See **Duplicates** |
| `possible_duplicate_of` | The id of a stored near match, else `null` |
| `note` | A short remark: why this bucket, these lists, or this duplicate mark |

**The printed line** is `name — action` (space, em dash, space). Its length,
`len(name) + 3 + len(action)`, must be at most `LINE_MAX`. When it is too long, shorten the
action first.

## Buckets

Read all four rubrics in `context.buckets` and pick the bucket whose rubric fits best. In short:

1. **Daily checklist**: worth doing every day or at every occurrence of a cue, quick, one clear
   physical action, and helped by a printed reminder where you are when it is due.
2. **Occasional activity**: worth doing now and then, but it takes real effort or time: a
   practice, a project, an outing, a periodic review.
3. **Optional / when time allows**: reasonable but not a priority: a one-time setup, a general
   principle without one action, a situational hack, a nice-to-have.
4. **Left out**: not worth doing, questionable or unsafe, or a duplicate of a stored tip.

Check bucket 4 first: a duplicate, or unsafe or questionable advice, is left out whatever else it
is. Left-out tips are kept, never dropped, so the operator sees them and they still catch later
duplicates.

## Audience

- `"kids+"` is fine for kids and everyone older. `"adults"` is for adults only.
- When unsure, pick `"adults"`: it can never put an unsuitable tip on a kids list.
- Coffee, alcohol, social media, work and phone tips are `"adults"`.

## Placement

Only bucket-1 tips go on lists. Buckets 2, 3 and 4 always have `list_ids: []`.

- A daily tip goes on the list whose **cue** matches *where you are when the action is due*, not
  where the action happens. Example: "lay out tomorrow's clothes" is due at bedtime, so it goes
  on the bathroom list whose cue is the bedtime routine, even though the clothes are in the
  bedroom.
- Read each list's cue, not just its place name.
- A tip may go on several lists, such as a leaving-the-house check on both the kids' and the
  adults' Front Door lists. It may also go on none: when no cue fits, use `list_ids: []` and the
  operator places it later.
- A kids list (`audience` `"kids"`) takes only `"kids+"` tips. An adults list takes both.
- Use only ids from `context.lists`.

## Duplicates

Compare every new tip with every tip in `context.tips`: pending and approved, from every source,
including the operator's own tips. (`context.tips` holds buckets 1 to 3 plus the left-out tips
that are not themselves duplicates, the rejected ideas.)

- **Duplicate**: the same action at the same cue or moment. A different reason, wording or source
  does not make it new. Set `bucket: 4`, `list_ids: []`, `duplicate_of: <the stored tip's id>`,
  and a note such as `duplicate of #12: same action at the same moment`. When several stored tips
  match, name the lowest id: the earlier tip always wins. A match against a rejected idea is a
  duplicate of that idea.
- **Near match**: the same goal at a different cue, such as "stretch while the kettle boils"
  against "stretch at the top of every hour". Keep the new tip's own bucket and lists and set
  `possible_duplicate_of: <the stored tip's id>`; the operator decides.
- A shared topic alone is neither. Leave both fields `null`.
- Both fields name ids from `context.tips` only. Tips in the proposal you are writing have no ids
  yet.

## Naming and polarity

- **Chaptered YouTube video:** by default the name is the chapter title, verbatim: the same
  words, capitals and punctuation, apostrophes included, as in `context.source.chapters`. A title
  that does not say what to do is still kept; the action line carries the meaning.
- **Renames.** Rename a chapter title only when it would be unsafe read literally, or when it
  names a harm. A checklist line reads as an instruction, so printing a harm's name tells the
  reader to do the harm. The new name states the healthy behavior and must differ from the title
  in its words, not just in capitals or punctuation. Keep the original title in `locator`.
  - "Staring At The Sun" → "Morning Daylight, Five Minutes" (unsafe read literally)
  - "Skipping Breakfast" → "Eat A Real Breakfast" (names a harm)
- A chapter title longer than `NAME_MAX` is shortened to fit, with the original in `locator`.
- **Other sources** (a video without chapters, a text, or an investigated article): write the
  name yourself: short, catchy, in Title Case. In an investigation the heading goes in `locator`,
  never in the name.
- **Action:** always states the healthy behavior, even when the title names the harm. For
  "Skipping Breakfast" the action is "Eat something with protein before you leave the house", not
  anything about skipping.

## The proposal file

One JSON object, UTF-8 without a byte-order mark. Every field is required and no other field is
allowed:

| Field | Value |
|---|---|
| `schema` | `"checklist-care.proposal/1"` |
| `source_ref` | The `source_ref` that `fetch` printed (also `context.source.ref`) |
| `packet_sha256` | Copied from `context.source.packet_sha256` |
| `skill_sha256` | Copied from `context.skill_sha256` |
| `host` | `"claude-code"` or `"codex"`: the value your adapter names |
| `model` | The model name your host reports, as free text |
| `mode` | `"investigate"` for a Wikipedia article investigated on the web (core step 6), else `"extract"` |
| `degraded` | `true` only for an investigation run without web search (core step 6), else `false` |
| `tips` | The tips, in source order (in heading order for an investigation); `[]` when the source holds no advice. At most `TIPS_MAX_EXTRACT` in extract mode and `TIPS_MAX_INVESTIGATE` in investigate mode |

Two examples follow. Both are self-written, and every id, title, heading, page and tip in them is
made up.

The first extracts a video, `EXAMPLE0001`. It shows a chapter title kept as the name and placed
on two lists, a renamed title that no list's cue fits, an occasional activity and a one-time
setup.

```json
{
  "schema": "checklist-care.proposal/1",
  "source_ref": "youtube:EXAMPLE0001",
  "packet_sha256": "<copied from context.source.packet_sha256>",
  "skill_sha256": "<copied from context.skill_sha256>",
  "host": "claude-code",
  "model": "<the model name your host reports>",
  "mode": "extract",
  "degraded": false,
  "tips": [
    {
      "name": "Shoes By The Door",
      "action": "When you come home, line up your shoes by the door",
      "reason": "A fixed spot for shoes saves the hunt when everyone is trying to leave in the morning.",
      "bucket": 1,
      "audience": "kids+",
      "list_ids": [5, 6],
      "start_s": 0,
      "locator": null,
      "evidence_url": null,
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "daily, seconds long, due when coming home through the front door"
    },
    {
      "name": "Eat A Real Breakfast",
      "action": "Eat something with protein before you leave the house",
      "reason": "A filling breakfast keeps energy steady through the morning.",
      "bucket": 1,
      "audience": "kids+",
      "list_ids": [],
      "start_s": 95,
      "locator": "Skipping Breakfast",
      "evidence_url": null,
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "renamed: the title names the harm; daily, but no list's cue is at the table"
    },
    {
      "name": "Fix One Broken Thing",
      "action": "Once a month, repair one small broken thing at home",
      "reason": "Small repairs done regularly stop little annoyances from piling up.",
      "bucket": 2,
      "audience": "adults",
      "list_ids": [],
      "start_s": 210,
      "locator": null,
      "evidence_url": null,
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "monthly, takes real time and tools"
    },
    {
      "name": "A Hook For Every Bag",
      "action": "Put up one hook by the door for each bag in the house",
      "reason": "Bags with a home stop cluttering the floor, but this is a one-time setup, not a daily step.",
      "bucket": 3,
      "audience": "adults",
      "list_ids": [],
      "start_s": 330,
      "locator": null,
      "evidence_url": null,
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "one-time setup, then nothing to repeat"
    }
  ]
}
```

The second investigates the Wikipedia article of a book, `Example_Book`, whose section headings
include `Ch.1: Start Smaller Than You Think`, `Ch.2: Let The Room Remind You` and
`Ch.3: One Thing At A Time`. Each tip names its heading in `locator` and the page it came from in
`evidence_url`: the author's own site, a summary, an interview, and the article itself for a tip
taken from the article's text.

```json
{
  "schema": "checklist-care.proposal/1",
  "source_ref": "wiki:en:Example_Book",
  "packet_sha256": "<copied from context.source.packet_sha256>",
  "skill_sha256": "<copied from context.skill_sha256>",
  "host": "claude-code",
  "model": "<the model name your host reports>",
  "mode": "investigate",
  "degraded": false,
  "tips": [
    {
      "name": "One Push-Up Before The Shower",
      "action": "Do one push-up before you step into the shower",
      "reason": "A start too small to skip makes the habit stick; the author says to grow it only later.",
      "bucket": 1,
      "audience": "adults",
      "list_ids": [1],
      "start_s": null,
      "locator": "Ch.1: Start Smaller Than You Think",
      "evidence_url": "https://example.org/author/start-smaller",
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "daily, seconds long, due while getting ready in the bathroom"
    },
    {
      "name": "Water Bottle By Your Bag",
      "action": "Fill your water bottle and set it next to your bag",
      "reason": "Putting what you need where you will see it makes the good choice the easy one.",
      "bucket": 1,
      "audience": "kids+",
      "list_ids": [5, 6],
      "start_s": null,
      "locator": "Ch.2: Let The Room Remind You",
      "evidence_url": "https://example.net/summaries/example-book",
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "daily, due when leaving the house"
    },
    {
      "name": "Weekly Room Reset",
      "action": "Once a week, put one room's things back where they belong",
      "reason": "A regular reset keeps the room's reminders in place, but it takes real time.",
      "bucket": 2,
      "audience": "kids+",
      "list_ids": [],
      "start_s": null,
      "locator": "Ch.2: Let The Room Remind You",
      "evidence_url": "https://example.com/interviews/rooms-that-remind",
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "weekly, takes real time"
    },
    {
      "name": "One New Habit At A Time",
      "action": "Work on one new habit at a time, never several at once",
      "reason": "The article sums up the chapter this way; it is a principle, not a daily step.",
      "bucket": 3,
      "audience": "adults",
      "list_ids": [],
      "start_s": null,
      "locator": "Ch.3: One Thing At A Time",
      "evidence_url": "https://en.wikipedia.org/wiki/Example_Book",
      "duplicate_of": null,
      "possible_duplicate_of": null,
      "note": "a principle; taken from the article's own text"
    }
  ]
}
```

In a degraded investigation the proposal has `"degraded": true` and every tip's `evidence_url` is
the article URL, copied exactly from the packet's `url`.

The list ids in the examples are only illustrations: always take ids, places, audiences and cues
from `context.lists`.
