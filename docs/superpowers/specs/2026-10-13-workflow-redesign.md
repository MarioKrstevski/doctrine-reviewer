# Doctrine Editor — Workflow redesign (Linear-style), gap analysis, edge cases

Date: 2026-10-13
Status: proposed, awaiting Mario's decisions (end of document). Builds on
`2026-10-01-online-deck-and-sync-design.md`.

## Incident that shaped this (2026-10-13)

Building release 1.1 stored a 2.15 GB `.apkg` on the 4.6 GB volume next to 2.0 GB of
media. The volume reached 0 bytes free and every database write failed (logins,
suggestions, accepts). Fixed by deleting the package (regenerable from the 1.1
collection file + media) and a 100 MB leftover test upload row. Root cause: the
storage estimate counted ~110 MB per release and ignored the stored package.
Decision below: never store packages; stream them per download.

## Target model

One add-on, two functions: **suggest an edit** and **update the deck**.
One platform, one domain, two faces:

- **Public**: Updates (release notes) and Install (download + guide). No staff links.
- **Staff** (after sign-in): a left sidebar in this order, which is the work order.

| # | Page | Purpose |
| --- | --- | --- |
| 1 | Inbox | Open suggestions. Student's card, our card, their proposed diff. Accept proposal / Edit / Decline / Expired. Banner: "Accepting adds this to the next release (1.2)". |
| 2 | Cards | Find a card by note id, guid or tag (text search optional, slow is fine). Editor + **History**: every change with date, who, source, diff; suggestions on this card; view the card as of any release. |
| 3 | Next release | Staging. Every accepted change, tagged by source (suggestion / direct edit / upload) and type (typo, incorrect, …). All ticked by default; untick to hold back; open any to refine. |
| 4 | Release | Minor or major (suggests 1.2 or 2.0), public notes or silent release, final summary, type the version to confirm, build. Becomes current for the add-on and buyer downloads. |
| 5 | Outbox | Thank-yous for contributors who left an email, each linked to the issue. Issues are never deleted. |
| 6 | Deck | Upload a new deck, or re-upload an existing one (diff becomes upload-tagged changes in Next release). Deck list, multi-deck ready. |
| 7 | Settings | Reviewers, buyer download links, installs (usage, sharing flags). |

## Gap analysis

| Area | Today | Change |
| --- | --- | --- |
| Suggest flow (add-on) | done: type, text, propose edit, optional email | none |
| Update flow (add-on) | done: counts + changelog link | prompt shows **your version vs newest** side by side and how many releases behind; pre-selected Update; install page warns local edits are overwritten |
| Public vs staff | staff link visible to anonymous visitors | public nav = Updates, Install only; staff nav after sign-in |
| Inbox | queue page works | rename, filters by type, "goes into release N" banner |
| Card editor | works | add **History** timeline and "as of release N" view |
| Card search | guid, note id, text | add **tags**; text stays (slow is fine) |
| Next release | grouped by type, selectable | add source tag (suggestion / direct / upload) as a second grouping |
| Release | cut + typed confirm + build | minor/major suggestion, **public notes or silent**, notes shown on Updates |
| **Release correctness** | **bug**: build and sync use the card's *current* text, so an edit made after cutting but before building leaks into the release | rebuild text from revisions up to that release (1.1 checked: not affected) |
| Downloads | stored 2 GB package per release (caused the incident) | **stream the package per download**, uncompressed zip (media is already compressed), exact length known up front; nothing stored |
| Per-buyer tag | none | each download carries a hidden `_doctrine_license.json` media file with a buyer token; Anki keeps `_` media on import; the add-on reports it |
| Installs / usage | install id sent only with suggestions | add-on sends install id, add-on version and deck release on each update check: last seen, version spread, sharing flags |
| Thank-yous | outbox works, links to tracking page | none beyond linking from History |
| Upload / re-upload | CLI tool + review page | browser upload on the Deck page (see decision 1) |
| Multiple decks | implicit single deck | `decks` table now, one row; add-on pinned to a deck via `/where`; notes, releases, official list and sync keyed by deck |
| External API | internal functions only | keep internal; card-state read API can be exposed later behind a token |

## Security, proportionate

- No backdoor to the deck exists today: full text only leaves through the pipeline-key
  endpoints; buyer links are 256-bit tokens with expiry; packages need a token or a
  staff session. Sync returns only changed cards.
- `/media/<name>` is public by filename. Needed for sync; anyone who can list the
  names already has the deck.
- The DEV add-on contains the pipeline key. Never share it; rotate the key if it leaks.
- Sharing detection: one license token seen from more than N installs (N≈3, to allow
  a second computer and reinstalls) is flagged. Detection only; deleting the hidden
  file defeats it; students who bought before tagging have no token.

## Edge cases

1. Edit made after a release is cut but before it is built leaks into it (bug above).
2. Replacing an image under the **same filename**: sync sees the student already has
   that name and skips it. Rule: a changed image gets a new filename; the editor enforces it.
3. A newer official deck whose note ids differ from v2: every student who imports it
   gets duplicates, and sync matches nothing. The uploader must report id overlap with
   the current deck and refuse a replacement that breaks ids unless it is a new deck.
4. "Clean slate" with a newer deck: decide what happens to test suggestions, outbox,
   history and releases (keep as archive, or wipe).
5. Two suggestions on one card, the second written against the old text: shown as
   "changed since submission" next to the current card.
6. Holding back one of two edits on the same card: selection is per card, so both
   ship together or neither does. Holding one back means reverting it.
7. Retired cards stay in students' collections; sync never deletes. Decide whether to
   mark them (e.g. a `Doctorine::Retired` tag would move them in the scheduler).
8. A release that adds notes of a note type the student does not have: sync cannot add
   them; they arrive only by re-importing a full package.
9. One person, two computers or a reinstall: several install ids on one license; the
   sharing threshold must allow it.
10. A student far behind (1.0 → 1.6): one jump straight to the newest, collapsed.
11. Refunds and expired links: revoking a buyer link stops downloads, not a deck already
    imported.
12. Contributor emails are personal data; credit names on the public page are
    opt-in (the field is optional and empty by default).

## Plan

| # | Work | Size |
| --- | --- | --- |
| 1 | Release-text bug + streamed downloads (buyer links work again, storage fixed for good) | 1 day |
| 2 | Staff shell: sidebar, public/staff split, Inbox banners, source tags | 1 day |
| 3 | Cards: History timeline, as-of-release view, tag search | 1 day |
| 4 | Release: minor/major, public notes or silent, notes on Updates | 1 day |
| 5 | Add-on: update prompt side by side, install ping; install page warning | 1 day |
| 6 | Deck page: browser upload new / re-upload | 2 days |
| 7 | Multi-deck model | 1–2 days |
| 8 | Per-buyer tag + sharing detection | 2 days |

## Decisions needed

1. **Browser re-upload needs ~2 GB of temporary space.** Grow the Railway volume, or
   keep re-uploads on the command line?
2. **Clean slate with the newer deck**: keep test history as an archive or wipe it; and
   do its note ids match v2 (checked automatically on upload)?
3. **Per-buyer tag via a hidden media file**, detection only: OK?
4. **Retired cards**: leave them alone, or tag them?
