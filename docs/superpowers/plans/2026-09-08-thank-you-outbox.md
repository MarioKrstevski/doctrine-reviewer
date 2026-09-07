# Doctrine Editor — Thank-you Outbox (P5) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reviewer-only page listing accepted suggestions whose submitter
left an email, with a pre-written thank-you ready to copy — so a human can
send it by hand until a mail sender exists. Nothing sends email.

**Architecture:** A `notifications` row is created inside the same
transaction that resolves a suggestion, only when an email is present.
`/outbox` renders pending rows with the suggestion, a length hint, the
rendered subject/body, a copy button, and Thanked / Skip actions. A single
`send()` stub exists behind `SEND_EMAIL`; enabling it without SMTP config
fails at boot, never silently.

**Tech Stack:** stdlib only.

**Covers spec phase:** P5.

---

## Task 1: Notification creation on resolve
- [ ] Tests: resolve + email → one pending row with rendered subject/body;
      resolve without email → none; decline/expire with email → none;
      resolving twice cannot duplicate.
- [ ] Schema via `_ensure_table`; `render_thank_you(suggestion)` pure;
      hook into `reviewer_action`.
- [ ] Commit.

## Task 2: `/outbox` page and actions
- [ ] Tests: requires login; lists pending only; shows suggestion text and
      length; `thanked` and `skipped` actions move rows out and record who;
      CSRF enforced.
- [ ] Implement `render_outbox`, `outbox_action`, routes, nav link with
      pending count.
- [ ] Commit.

## Task 3: Sender stub and boot guard
- [ ] Tests: `SEND_EMAIL` unset/false → no-op; `SEND_EMAIL=true` without
      `SMTP_URL` → startup raises with a clear message.
- [ ] `config.send_email`, `config.smtp_url`; `email_out.send()` stub.
- [ ] Commit.

## Task 4: Deploy and verify
- [ ] Deploy; resolve a suggestion with an email on the live host; confirm
      the outbox row; mark it thanked.

## Definition of done
- [ ] Resolving a suggestion with an email queues exactly one thank-you
- [ ] `/outbox` is behind login, shows only pending, supports thanked/skip
- [ ] Nothing sends mail; enabling sending without SMTP config cannot
      happen silently
