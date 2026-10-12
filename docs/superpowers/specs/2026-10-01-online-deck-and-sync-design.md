# Doctrine Editor — Online deck, editor, releases and sync

Date: 2026-10-01
Status: decisions agreed with Mario; open items listed at the end. Not yet implemented.
Supersedes the "master copy" parts of `2026-09-01-doctrine-editor-production-design.md`
and the identity notes in CLAUDE.md where they conflict.

## Where things actually stand

- The deck was built in CardSmith, exported, then edited via CSV with Claude by one
  person. That result is **Doctorine v2**, the deck on sale. Nothing keeps an online
  copy of it; CardSmith is not the master and is not kept in step.
- The CSV import is why note IDs changed between Doctrine 0.1 (CardSmith ObjectIds)
  and v2 (native Anki guids). Every paying student now has the v2 guids.
- Doctorine v2: 32,279 notes in one deck, 32,276 on `AnKingOverhaul++++++`, 3 on
  `Doctorine v2`; ~3,045 media files; the .apkg is 2–3 GB.
- The Study Navigator scheduler works on whatever notes a student has, by tag. Its PDF
  feature only selects existing cards that match a lecture; it never creates cards.
- The platform already stores all 32,279 notes (fields + tags, keyed by guid) from
  registering v2. It does not have note types, templates, CSS or media.

## Goal

One online master of the Doctorine deck, owned by the platform, from which:
staff and reviewers edit cards and add notes; each version is released as an .apkg
for new buyers; existing students update in place through the add-on.

## Decisions (agreed)

1. **Identity is the v2 note guid**, permanently. No custom ID field and no "secret"
   tags. Both are allowed later if a need appears; neither is needed now. Metadata we
   want about a card lives in our database keyed by guid.
2. **The platform is the only place the deck is edited.** The CSV-and-Claude
   workflow stops. Edits made elsewhere come back only through *re-upload with diff*
   (below), never by replacing the online deck wholesale.
3. **The review tool covers official notes only**: notes whose guid is in the latest
   release. Students' own notes, notes added on their machine, copies of official
   notes (new guid), and other decks are out of scope: no button, and the server
   rejects them. Deck name and tags are not the test.
4. **Sync updates `Text` and `Extra` only**, matched by guid, and **overwrites local
   edits**. Students are told, at install and in the sync prompt, not to edit cards
   and to request a change instead. Sync never touches scheduling, tags, note types
   or other fields.
5. **Tags are the schedule.** Content releases do not change tags. Tag changes, if
   ever needed, are a separate, announced event coordinated with the scheduler.
6. **Note type changes are one-off events** (they force a full sync for every
   student). Releases keep note type ids, names and field order exactly as v2 has
   them. The move from `AnKingOverhaul++++++` to `Doctorine v2` is not part of this
   work.

## Components

### 1. Official-card gate

- Server: `/api/suggestions` rejects a guid that is not in the current release
  (restores the gate removed on 2026-09-22, now that a master exists).
- Add-on: fetches the official guid list for the current release (one request per
  release, cached in `user_files/`, ~350 KB) and shows the button only on those notes,
  wherever the student moved them. Falls back to "button shown, server decides" if
  the list cannot be fetched.
- Replaces the deck-name and `Doctorine ID`-field gate.

### 2. Online deck — data model

Extends the existing `notes` table; nothing is dropped.

| table | holds |
| --- | --- |
| `note_types` | Anki note type id, name, field names in order, templates, CSS |
| `notes` | guid (PK), note type id, fields, tags, `retired`, `added_in` release |
| `media` | filename, sha256, size, object-storage key |
| `releases` | version, status (`draft` / `published`), created by/at, .apkg key, changelog |
| `revisions` | guid, field, before, after, author, source (`suggestion:<id>`, `upload:<id>`, `staff`), release (NULL = pending) |

"Pending changes" = revisions with no release yet. The current text of a note is its
last revision applied over the baseline.

### 3. Baseline import of Doctorine v2

A local command, run once on Mario's machine against the .apkg:
`python3 tools/import_apkg.py "Doctorine v2.apkg"`. It reads the file locally
(decompressing `collection.anki21b`), uploads note types, notes and media
(media to object storage, metadata to the platform with the pipeline key), and
records it as **release 1.0**. No 3 GB upload through a browser.

### 4. Re-upload with diff

For edits that happen outside the platform anyway. Same command or a staff page.

- Compares every guid in the file with the online deck: **new**, **changed**
  (per field), **missing from the file**, **unchanged**.
- Shows a diff page; nothing is applied until a staff member confirms.
- **Refuses** the upload if most guids do not match: the export broke the IDs.
- **Conflicts**: a note changed both in the file and on the platform since the
  file's base release is shown side by side; staff pick a version per note.
- Notes missing from the file are never deleted; staff can mark them retired.
- Confirmed changes become pending revisions with `source = upload:<id>`.

### 5. Editor

On a suggestion: **Accept as proposed** (when the student proposed an edit),
**Edit** (Text and Extra in the browser, cloze markers highlighted, rendered preview
with media), or **Decline**. Staff can also **add a note** (note type, fields, tags).
Every edit records author and source, and lands as pending.

The add-on's suggest dialog gains an optional "propose an edit": an editable copy of
Text and Extra, sent as before/after. Free-text suggestions stay.

### 6. Release

"Cut release" turns pending revisions into version N, builds the .apkg, stores it in
object storage, and publishes the changelog to `/updates`.

The .apkg builder must keep, exactly as v2: guids, note type ids and names, field
names and order, the deck id and name. A changed note type id makes Anki create a
duplicate note type on import; a changed guid duplicates the note. Built once per
release, never per download.

### 7. Downloads for new buyers

The buy flow ends on a platform link that serves the latest published release. The
link must be tied to a purchase (see open items), or the paid deck is public.

### 8. Sync for existing students (add-on)

- On startup the add-on asks for the latest release. If newer than its local
  version, it shows: "Release N: X cards updated. [Update now] [Later]", with a link
  to the changelog and the line "Updating replaces any edits you made to these cards."
- Update: fetch changed notes since the local version (guid, Text, Extra), apply via
  `mw.col.update_note` on the main thread, download any media those fields reference
  and is missing locally, then store the new version in `user_files/`.
- Notes the student deleted are skipped and logged. Nothing else is touched.

### Storage (decided 2026-10-12: stay on the volume)

Media (2.0 GB, 3,045 files, stored once by sha256) and release collection files
(~110 MB each) live on the Railway volume at `/data/blobs`. Database ~30 MB.
Volume is 4.6 GB, 50% used after release 1.0.

Why not R2 now: reviewer traffic is tiny (target 10,000 students, ~5% ever
suggesting, each item viewed a few times by a reviewer); storage grows only with
new media and ~110 MB per release, so ~20 releases of headroom; and the deck is
expected to need roughly one correction pass. Loss of the volume is recoverable:
every shipped .apkg contains the same media and `tools/import_apkg.py` restores it
in ~30 minutes.

What would change the decision: serving the full 2 GB release .apkg to buyers from
the container (phase 5 may put release files on R2 while media stays), or a volume
loss becoming unacceptable. Switching is a backend swap in `releases.py` plus a
one-off copy; nothing done now makes it harder. If R2 is used, serve through the
platform (`/media/<name>`), no custom domain on the bucket, keep the `media` table
as the manifest, so the bucket can be copied to the client's account in minutes.

## Phases

| phase | scope | rough size |
| --- | --- | --- |
| 1 | Official-card gate (server + add-on) | 1–2 days |
| 2 | Baseline import incl. note types and media; release 1.0 | 2–3 days |
| 3 | Editor, add-note, proposed edits from students | ~1 week |
| 4 | Re-upload with diff | 3–4 days |
| 5 | Release builder + purchase-gated download | ~1 week |
| 6 | Sync in the add-on | 1–2 weeks |

Each phase ships and is usable on its own. Sizes are estimates before a plan is
written per phase.

## Open items

1. **New notes via sync**: should sync add notes created after release 1.0 to
   existing students, or only update Text and Extra on notes they already have?
   (If only updates: new notes reach existing students by re-importing a release.)
2. **Purchases**: how does Stripe record a buyer today, and what can the download
   link check against (customer list, license key, checkout session)?
3. **Object storage**: R2 or S3, under which account.
4. **Roles**: may every reviewer edit and cut releases, or only some (an `editor`
   role)?
5. **Release cadence**: monthly fixed date, early for urgent fixes (proposed).

## Risks

- A release built with a different note type id or field order silently creates
  duplicate note types for every student who imports it. The builder has to be tested
  against a real v2 import before release 1.1 ships.
- Sync is the first code that writes to students' collections. It needs a dry-run
  mode, a log, and testing on a copy of a real collection before it reaches anyone.
- Until the gate (phase 1) ships, the button shows on any note in the Doctorine deck,
  including students' own notes.
