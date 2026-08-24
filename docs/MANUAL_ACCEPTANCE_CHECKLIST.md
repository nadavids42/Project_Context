# Manual acceptance checklist

Matches the product plan, Section 15 ("Manual acceptance checklist"):
"Run on a clean Ubuntu user/profile." This is the part automated tests
cannot cover — a real OAuth consent screen, a real Fathom API key,
visual review of a live sync, restart behavior, and a final read-through
of what actually ended up on disk and in logs. Run this before calling
a build ready for dogfooding (Section 19.1) or handing it to anyone
else (Section 19.2's "Setup documentation works on a clean
environment" is proven by step 1 below, specifically).

Use only synthetic, personal, public, or explicitly authorized data —
never employer or customer data (README's privacy/data policy banner
repeats this every time the app runs; it is not optional here either).

Record the date, git commit, and outcome of each step. A step that
fails does not necessarily block a checkpoint — but it must be recorded
as a known issue (see `docs/RELEASE_NOTES.md`), not silently skipped.

---

## 1. Install and start from README

- [ ] On a clean Ubuntu user/profile (a fresh VM or a new local user is
      fine — the point is no pre-existing `.venv`, `data/`, or
      environment variables from prior work), follow the README's
      **Install** and **Run** sections exactly as written, with no
      undocumented steps.
- [ ] `streamlit run app.py` starts and opens the Projects page with no
      configuration beyond what README's Install section describes.
- [ ] The privacy banner is visible and its wording matches what the
      app actually does right now (Section 16: "The UI must say
      precisely...").

## 2. Create two projects

- [ ] Create two projects with clearly different names/objectives.
- [ ] Confirm both appear on the Projects list, each opens to its own
      Project Overview, and switching between them never shows the
      other project's data anywhere (title bar, evidence, ledger).

## 3. Upload each supported file type

For **each** project, upload one of each: `.txt`, `.md`, `.docx`,
`.pdf` (a text PDF), and `.vtt`.

- [ ] Each appears in the Evidence list with the correct parse status.
- [ ] The Evidence viewer shows normalized text for each, and a
      deliberately-scanned/image-only PDF (if you have one) is flagged
      `ocr_required` rather than shown as empty/misleading.
- [ ] Re-uploading the exact same file a second time is reported as no
      change (idempotent), not a duplicate.

## 4. Sync one configured live connector with authorized test data

- [ ] Configure and connect **one** live connector (Drive is the
      simplest — see README's "Google Drive setup") against a real
      OAuth test-mode client and content you are personally authorized
      to use — **never** employer/customer data.
- [ ] Click **Sync Project**; confirm discovered/unchanged/downloaded/
      parsed/extracted/failed/unassigned counts render and look
      correct for what you actually put in the folder.
- [ ] Run the sync a second time with no changes upstream; confirm it
      reports unchanged, not duplicated evidence.

## 5. Review every action type

Using either the connector sync above or manual uploads worded to
trigger each case, exercise every review action at least once from the
Activity & Review queue:

- [ ] Accept (create)
- [ ] Edit + accept (a corrected field — confirm it appears as a
      `corrections` row)
- [ ] Reject
- [ ] Mark complete
- [ ] Mark superseded
- [ ] Treat as new

## 6. Generate both briefs and open every citation

- [ ] Generate a Current Project Brief; open every citation link it
      contains and confirm each lands on the exact cited evidence span.
- [ ] Generate a Meeting Preparation Brief for a real or manually
      entered meeting; confirm the previous-meeting cutoff shown is
      correct and every citation resolves the same way.
- [ ] Confirm inferences/suggestions are visibly labeled as such, never
      presented as plain fact.

## 7. Restart; confirm state/history

- [ ] Stop the Streamlit process entirely and start it again.
- [ ] Confirm both projects, all evidence, the full ledger (current
      state and version history), all reviews/corrections, and both
      generated briefs are exactly as they were before the restart.

## 8. Revoke connector; confirm safe failure and recovery

- [ ] In the connector's real provider console (e.g. Google Cloud
      Console for Drive), revoke this application's access.
- [ ] Click **Sync Project** again; confirm the source moves to
      `reauth_required` with a clear, safe (non-raw-error) message —
      not a crash, not a silent no-op.
- [ ] Reconnect; confirm sync recovers normally.

## 9. Delete a test project; confirm evidence/credentials/FTS cleanup

- [ ] On a **test** project you're willing to lose (seed a throwaway
      one if needed), click **Delete**, confirm the preview counts
      look right, type the exact project name, and confirm.
- [ ] Confirm the project disappears from both the active and archived
      lists.
- [ ] Confirm any connector this project had connected is disconnected
      (its credential is gone — check Sources & Settings shows "Not
      connected" if you reopen it, which you shouldn't be able to,
      since the project itself is gone; verify via a fresh connector
      list instead, or by inspecting `data/credentials/` before vs.
      after if you used the encrypted-file fallback).
- [ ] Confirm a full-text search for a distinctive phrase from that
      project's deleted evidence/ledger finds nothing in any other
      project's Evidence/Ledger search.
- [ ] Separately, confirm **Archive** on a *different* project does
      **not** delete anything — restore it afterward and confirm all
      its data is intact.

## 10. Inspect logs and repository for secrets/content

- [ ] Read through the terminal/log output from this entire session.
      Confirm no evidence quote, source body text, API key, OAuth
      token, or `Authorization` header value appears anywhere in it.
- [ ] Run `git status` (if this profile has its own clone) and confirm
      `data/`, `.env`, and any credential file are all gitignored, not
      staged.
- [ ] Run `pytest tests/unit/test_secret_scan.py tests/security/` and
      confirm both pass.

---

## Optional extras beyond Section 15's ten items

These aren't in the product plan's original ten-item list but were
added by this checkpoint's backup/deletion hardening — worth running
once per checkpoint, not required every time:

- [ ] `python scripts/backup.py backup --dest <somewhere>` against your
      real `data/` directory, then `python scripts/backup.py verify
      --backup-dir <the result>` — confirm it reports OK.
- [ ] Restore that backup into a **scratch** directory (never your real
      `data/`) with `python scripts/backup.py restore ... --target-data-dir
      /tmp/scratch-restore` and spot-check the restored data looks
      right (or just trust `tests/unit/test_backup_restore.py`, which
      does this automatically on every `pytest` run).
