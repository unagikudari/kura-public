#!/usr/bin/env python3
# Description: consolidated tagger worker entrypoint — profile-dispatches to loop.py (full) or nodec_autotag_worker.py (wd14)
# Kind: worker
# Effects: mutating
# Runs-on: primary (profile=full) / nodec,nodeb,desk (profile=wd14)
"""tagger/worker.py — one entrypoint, one systemd unit template (plan:kura-
tagger-lifecycle-redesign Step 12, "同名 service の場所依存を根絶").

Before this, `kura-tagger.service` meant "the PixAI+ml-danbooru+WD14 full
pipeline" on primary but was ALSO deployed (hand-copied, not repo-tracked) on
nodec/nodeb/desk as a completely different, older worker — same unit name,
different code, different behavior depending on which host happened to run
it. That ambiguity is exactly what raced against the newer
`kura-autotag.service` on nodec/nodeb and caused non-deterministic per-post
model selection (2026-07-20, this session). Both are now masked (Step 4).

This module does NOT reimplement either ML pipeline. loop.py's full
ensemble (PixAI + ml-danbooru + wd14-fixed + smegma + content-heads) and
nodec_autotag_worker.py's WD14-only release_client.py-backed pipeline are
both production-tuned, live-verified code — rewriting them from scratch
here would risk silently regressing tagging quality with no way to notice
until real posts came out wrong. Instead this is a thin profile dispatcher:
one systemd template (tagger/systemd/kura-tagger@.service) with the
instance name (%i) selecting TAGGER_PROFILE, so the SERVICE NAME itself
now encodes which pipeline runs — `kura-tagger@full.service` can only ever
mean the full ensemble, `kura-tagger@wd14.service` can only ever mean
WD14-only, regardless of which host it's instantiated on. There is no more
"kura-tagger.service means something different depending on where you
look."

Both loop.py and nodec_autotag_worker.py already send the claim-guard
identity fields (worker_id/profile/generation/release_id/provider/
model_sha256, Step 11) required by api/routers/tagging.py::claim for
kind=auto_tag -- api/services/tagging.py::enforce_claim_guard rejects a
worker whose reported profile doesn't match its host's
tagger_expected_worker.profile row, so a misconfigured TAGGER_PROFILE (or
copy-pasting this unit to the wrong host) fails the very first claim with
a 409, not silent wrong-model tagging.

Rollout for a systemd-unit swap on a currently-running node still needs
the plan's staged procedure (shadow -> single-node canary -> diff eval ->
queue-ownership switch -> old unit mask/delete confirm -> rollback
rehearsal) before any node's queue ownership actually moves to this
entrypoint -- that requires live worker traffic and is out of scope for a
stopped fleet; see kura-tagger-lifecycle-redesign Step 12 notes.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    profile = os.environ.get("TAGGER_PROFILE")
    if not profile and len(sys.argv) > 1 and sys.argv[1] in ("full", "wd14"):
        # Manual-testing convenience only — strip it so nodec_autotag_worker's
        # own argparse (which reads sys.argv[1:]) doesn't choke on an
        # unrecognized positional. The systemd path never hits this branch
        # (Environment=TAGGER_PROFILE=%i sets the env var instead).
        profile = sys.argv.pop(1)
    if profile == "full":
        from . import loop
        return loop.main()
    if profile == "wd14":
        from . import nodec_autotag_worker
        return nodec_autotag_worker.main()
    print(
        "ERROR: TAGGER_PROFILE must be 'full' or 'wd14' (set via systemd "
        "instance name %i in kura-tagger@<profile>.service, or pass as argv[1])",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
