# VENDORED — do not edit here.

These files are byte-for-byte copies of the canonical refguard guards in the
**broker** repo (`scripts/devtools/refguard/`). kura's local pre-commit
uses the broker copy directly (absolute path, primary-only); these vendored copies
exist solely so kura's **GitHub Actions** CI — which runs on a cloud runner with
no broker checkout — can run the same guards (the unbypassable layer).

Sync / drift-check (run on primary, where both repos are local):

    python3 ~/broker/scripts/devtools/refguard/vendor.py sync
    python3 ~/broker/scripts/devtools/refguard/vendor.py check

The broker pre-commit runs `check` automatically when a guard changes, so these
stay in sync. Plan: entity-retirement-lifecycle P5 #1.
