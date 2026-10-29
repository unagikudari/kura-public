"""One variable held two roles whose requirements are opposites (kura#43-2).

`KURA_SECRET` was, at the same time:

    the session cookie signing key   — must be strong, must be rotatable (#59)
    the content filename salt        — must never change, ever (2M files)

Those cannot both be satisfied by one value, and the way it resolved was the worse
one: the value was never rotated, so it stayed a six-character dictionary word,
and that word was signing cookies carrying UserTokens.

The first attempt at this recorded every filename in a `post` column so the salt
could be rotated too. It hit a wall the reviews found — `post` is in
kura_mesh_pub, DDL is not replicated, trainer's copy is read-only, and kura-api
runs `alembic upgrade head` from its own lifespan, so no single node can add a
column quietly. The user ruled: redesign rather than patch around it.

The defect was never the column. It was one name for two jobs. So the variable is
split by role — the same shape as KURA_DASHBOARD_KEY earlier the same day — and
the salt is frozen and stops being called a secret. #58 established that the
unguessable filename protects nothing, because /api/post/{id}/content answers
without auth on the same reach.

No migration. No rename. No DDL on a replica.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _reload_config(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import api.config
    return importlib.reload(api.config)


def test_the_salt_has_its_own_variable(monkeypatch):
    cfg = _reload_config(monkeypatch, KURA_FS_NAME_SALT="abc", KURA_SECRET="zzz")
    assert cfg.FS_NAME_SALT == "abc"


def test_the_salt_does_not_fall_back_to_the_secret(monkeypatch):
    """THE SHAPE THAT CAUSED THIS. KURA_DASHBOARD_KEY fell back to KURA_SECRET
    under a comment saying the two were separate; that is how a credential became
    a filename salt in the first place. A fallback here would rejoin them."""
    cfg = _reload_config(monkeypatch, KURA_FS_NAME_SALT=None, KURA_SECRET="zzz")
    assert cfg.FS_NAME_SALT == ""


def test_the_derivation_reads_the_salt_not_the_secret(monkeypatch):
    """If this read KURA_SECRET, retiring that variable would silently rename
    two million files — which is the failure the whole redesign avoids."""
    _reload_config(monkeypatch, KURA_FS_NAME_SALT="salt-value", KURA_SECRET="other")
    import api.services.hashing
    hashing = importlib.reload(api.services.hashing)

    import hmac
    expected = hmac.new(b"salt-value", msg=b"42", digestmod="md5").hexdigest()[:16]
    assert hashing.get_security_hash(42) == expected


def test_changing_only_the_secret_does_not_move_a_filename(monkeypatch):
    """The property the split exists to create: KURA_SECRET becomes rotatable
    without touching a single name on disk."""
    _reload_config(monkeypatch, KURA_FS_NAME_SALT="frozen", KURA_SECRET="before")
    import api.services.hashing
    first = importlib.reload(api.services.hashing).get_security_hash(1234)

    _reload_config(monkeypatch, KURA_FS_NAME_SALT="frozen", KURA_SECRET="after")
    import api.services.hashing as h2
    assert importlib.reload(h2).get_security_hash(1234) == first


# ── the boot guard asks a different question now ────────────────────────────

def test_the_guard_refuses_when_the_salt_is_absent():
    """Unset means the addresses of two million files cannot be derived."""
    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    guard = src[src.index("def _check_fs_name_salt"):]
    guard = guard[:guard.index("async def _heal_stale_sequences")]
    assert "if not FS_NAME_SALT:" in guard
    assert "KURA_FS_NAME_SALT is not set" in guard


def test_the_guard_no_longer_tests_strength():
    """It used to compare KURA_SECRET against the literal "change-me" — a
    strength check on something believed to be a secret. It let a six-character
    dictionary word through, and that word was signing session cookies. The salt
    is not a secret (#58), so strength is not the question; presence is."""
    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    guard = src[src.index("def _check_fs_name_salt"):]
    guard = guard[:guard.index("async def _heal_stale_sequences")]
    code = "\n".join(l for l in guard.splitlines()
                     if not l.strip().startswith("#") and '"""' not in l)
    assert 'SECRET == "change-me"' not in code


def test_the_guard_tells_an_existing_deployment_what_to_set_it_to():
    """"Set KURA_FS_NAME_SALT" is useless advice here: any value starts the
    process and the wrong one 404s every file. It has to say: the current
    KURA_SECRET, because that is what the files are already named with."""
    src = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    assert "CURRENT value of KURA_SECRET" in src
    assert "not being" in src and "renamed" in src


# ── one derivation, not four ────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "api/routers/posts.py",
    "api/routers/upload.py",
    "api/routers/pools.py",
])
def test_no_router_carries_its_own_copy_of_the_derivation(rel):
    """Four byte-identical implementations agreed for years. The split is exactly
    the event that would have ended that: only the canonical one was changed to
    read the salt, so the other three would have kept answering with the old
    variable the moment it was retired or rotated."""
    src = (REPO / rel).read_text(encoding="utf-8")
    assert "hexdigest()[:16]" not in src
    assert "from api.config import" in src
    assert "SECRET" not in src.split("from api.config import")[1].split("\n")[0]


def test_the_dashboard_key_refuses_to_equal_either_of_them(monkeypatch):
    """The check named KURA_SECRET only. Retiring that variable would have
    dropped half the guard silently, and the half it kept would be the one that
    no longer names anything."""
    src = (REPO / "api" / "dashboard_key.py").read_text(encoding="utf-8")
    assert '"KURA_FS_NAME_SALT", "KURA_SECRET"' in src

    monkeypatch.setenv("KURA_FS_NAME_SALT", "shared")
    monkeypatch.setenv("KURA_DASHBOARD_KEY", "shared")
    monkeypatch.delenv("KURA_SECRET", raising=False)
    import api.dashboard_key
    dk = importlib.reload(api.dashboard_key)
    assert dk.configured_key() is None


def test_the_example_env_says_it_is_frozen_and_not_a_credential():
    """The next person to stand a node up needs to know that this value is not
    theirs to choose — an existing deployment's files are already named by it."""
    src = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "KURA_FS_NAME_SALT=" in src
    assert "NOT a credential" in src
    assert "renames two million files" in src


# ── round 2: what the adversarial review refused ───────────────────────────
#
# Both lanes returned REVISE on the same shape from two directions: presence is
# not correctness. Everything below fixes a hole one of them found, so each test
# names the hole rather than the code.

import os
import re


def _reload_files(monkeypatch, salt, data_dir):
    monkeypatch.setenv("KURA_FS_NAME_SALT", salt)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    import api.config, api.services.hashing, api.services.files
    importlib.reload(api.config)
    importlib.reload(api.services.hashing)
    return importlib.reload(api.services.files)


def _plant(data_dir, salt, ids, *, wrong=()):
    """Write filenames the way the real tree is named. Nothing is read back —
    only the names matter, which is the whole point of the check."""
    import hmac
    posts = data_dir / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    for pid in ids:
        key = ("WRONG" if pid in wrong else salt).encode()
        h = hmac.new(key, str(pid).encode(), digestmod="md5").hexdigest()[:16]
        (posts / f"{pid}_{h}.jpg").write_bytes(b"x")
    return posts


def test_a_salt_that_names_the_files_verifies(monkeypatch, tmp_path):
    _plant(tmp_path, "correct-salt", range(1, 6))
    files = _reload_files(monkeypatch, "correct-salt", tmp_path)
    assert files.verify_salt_names_the_files() == ("ok", 5, 5)


def test_a_wrong_salt_is_caught_even_though_it_is_not_empty(monkeypatch, tmp_path):
    """THE ROUND-2 BLOCKER (both lanes). `if not FS_NAME_SALT` passes a typo,
    another deployment's value, and the env template's own placeholder. Each one
    boots cleanly and 404s every FS-backed file — the 2026-04-20 incident,
    reached in silence. Only the disk can answer this question."""
    _plant(tmp_path, "the-value-the-files-were-named-with", range(1, 6))
    files = _reload_files(monkeypatch, "set-me-to-the-value-the-files-were-named-with", tmp_path)
    verdict, checked, matched = files.verify_salt_names_the_files()
    assert verdict == "mismatch"
    assert checked == 5 and matched == 0


def test_one_file_out_of_five_is_still_a_mismatch(monkeypatch, tmp_path):
    """Not a majority vote. A salt either names these files or it does not."""
    _plant(tmp_path, "correct-salt", range(1, 6), wrong={3})
    files = _reload_files(monkeypatch, "correct-salt", tmp_path)
    assert files.verify_salt_names_the_files()[0] == "mismatch"


def test_nothing_to_check_against_is_not_a_pass(monkeypatch, tmp_path):
    """A new deployment has no files, so the check cannot run. It must say that
    rather than return the same answer as a verified node — unknown is never
    green, or a wrong salt on a fresh node looks identical to a proven one."""
    (tmp_path / "posts").mkdir()
    files = _reload_files(monkeypatch, "anything", tmp_path)
    assert files.verify_salt_names_the_files()[0] == "no-files"


def test_a_missing_data_dir_does_not_crash_the_boot(monkeypatch, tmp_path):
    files = _reload_files(monkeypatch, "anything", tmp_path / "nope")
    assert files.verify_salt_names_the_files()[0] == "unreadable"


def test_could_not_look_is_not_the_same_answer_as_looked_and_found_none(
        monkeypatch, tmp_path):
    """媒体を持たないノード (正常) と、volume 未 mount / 権限退行 (設定の異常) を
    同じ verdict に畳むと、誤った salt のまま「新規デプロイなら正常」とログして
    serve し、新規 upload が誤 salt 下に書かれて mixed-salt な木を自分で作る
    (2026-09-04 の敵対的レビュー)。"""
    (tmp_path / "posts").mkdir()
    files = _reload_files(monkeypatch, "anything", tmp_path)
    assert files.verify_salt_names_the_files()[0] == "no-files"

    notdir = tmp_path / "as-a-file"
    notdir.mkdir()
    (notdir / "posts").write_text("not a directory")
    files = _reload_files(monkeypatch, "anything", notdir)
    assert files.verify_salt_names_the_files()[0] == "unreadable"

    noperm = tmp_path / "no-permission"
    (noperm / "posts").mkdir(parents=True)
    (noperm / "posts" / "1_0123456789abcdef.jpg").write_text("x")
    (noperm / "posts").chmod(0o000)
    try:
        files = _reload_files(monkeypatch, "anything", noperm)
        assert files.verify_salt_names_the_files()[0] == "unreadable"
    finally:
        (noperm / "posts").chmod(0o755)


def test_the_check_never_reports_a_filename_or_a_hash(monkeypatch, tmp_path):
    """The 16 hex is HMAC-MD5 of a known post_id under the salt. Logging one
    beside its id hands anyone with the log an offline crack of a short salt —
    and the salt is still equal to the retiring KURA_SECRET on live nodes. So
    the function returns counts, and nothing that was read."""
    _plant(tmp_path, "correct-salt", range(1, 6))
    files = _reload_files(monkeypatch, "correct-salt", tmp_path)
    result = files.verify_salt_names_the_files()
    assert all(isinstance(x, (str, int)) for x in result)
    assert result[0] in ("ok", "mismatch", "reader-absent", "reader-missing", "no-files", "unreadable")
    assert not re.search(r"[0-9a-f]{16}", " ".join(str(x) for x in result))


def test_the_guard_refuses_a_wrong_salt_and_says_do_not_generate_one():
    src = (REPO / "api" / "main.py").read_text()
    guard = src[src.index("def _check_fs_name_salt"):]
    guard = guard[:guard.index("\nasync def ")]
    assert "verify_salt_names_the_files" in guard
    assert "mismatch" in guard
    assert "do not generate a new one" in guard.lower()


def test_the_guard_says_out_loud_when_it_could_not_verify():
    """Silence on 'no-files' would make an unverified node indistinguishable
    from a verified one in the log."""
    src = (REPO / "api" / "main.py").read_text()
    guard = src[src.index("def _check_fs_name_salt"):]
    guard = guard[:guard.index("\nasync def ")]
    assert "no-files" in guard and "NOT verified" in guard


def test_the_guard_separates_could_not_look_from_nothing_to_look_at():
    """「見られなかった」を正常な姿と同じ声で言わない — 設定の異常として名指す。"""
    src = (REPO / "api" / "main.py").read_text()
    guard = src[src.index("def _check_fs_name_salt"):]
    guard = guard[:guard.index("\nasync def ")]
    assert "unreadable" in guard and "COULD NOT BE CHECKED" in guard
    assert "_log.error" in guard, "正常な姿と同じ warning では埋もれる"


def test_the_derivation_refuses_on_its_own(monkeypatch):
    """FOUND IN REVIEW. The boot guard runs in the FastAPI lifespan, and
    scripts/, workers and CLI tools never enter it — they imported the
    derivation, hashed with "", and built paths that simply do not exist.
    export_censor_gt.py dropped every image as "unavailable" with no error. A
    guard one import away from what it guards is a guard with holes."""
    monkeypatch.delenv("KURA_FS_NAME_SALT", raising=False)
    import api.config, api.services.hashing
    importlib.reload(api.config)
    hashing = importlib.reload(api.services.hashing)
    with pytest.raises(RuntimeError) as e:
        hashing.get_security_hash(1)
    assert "KURA_FS_NAME_SALT" in str(e.value)


@pytest.mark.parametrize("rel", [".env.example", "api/env.example"])
def test_every_env_template_that_names_the_old_secret_also_names_the_salt(rel):
    """THE HEAVIEST ROUND-2 FINDING. I updated .env.example. systemd reads
    api/env, so api/env.example is the file a new node actually copies — and it
    still said KURA_SECRET=change-me and nothing else. Parametrized over the
    templates rather than fixed to the one that was wrong, because the failure
    was 'there is more than one of these', not 'this one was stale'."""
    text = (REPO / rel).read_text()
    if "KURA_SECRET" not in text:
        pytest.skip(f"{rel} does not configure the retiring secret")
    assert "KURA_FS_NAME_SALT" in text, (
        f"{rel} configures KURA_SECRET but not KURA_FS_NAME_SALT. A node "
        f"started from this template derives no filename at all.")


@pytest.mark.parametrize("rel", [".env.example", "api/env.example"])
def test_no_template_ships_a_non_empty_placeholder_salt(rel):
    """A placeholder is exactly the wrong-but-non-empty value the guard is now
    built to reject; shipping one invites a boot that 404s everything. Empty is
    the honest default — the guard refuses it and says what to do."""
    for line in (REPO / rel).read_text().splitlines():
        if line.startswith("KURA_FS_NAME_SALT="):
            assert line.strip() == "KURA_FS_NAME_SALT=", (
                f"{rel} ships a non-empty placeholder salt")


@pytest.mark.parametrize("rel", [".env.example", "api/env.example"])
def test_the_templates_say_which_reader_you_are(rel):
    """Review: 'set it to the value the files were named with' reads one way to
    someone building a new cluster and another way to someone adding a node to
    an existing one, and the second reader inventing a value loses everything."""
    text = (REPO / rel).read_text().lower()
    if "KURA_FS_NAME_SALT" not in (REPO / rel).read_text():
        pytest.skip(f"{rel} does not configure the salt")
    assert "existing cluster" in text and "do not generate" in text


def test_the_content_endpoint_is_still_unauthenticated():
    """The claim 'the salt is not a credential' is not free-standing — it rests
    on #58's measurement that /api/post/{id}/content answers without auth on the
    same reach as /data, so the unguessable name guards nothing that is not
    already open. If someone authenticates that endpoint while /data stays
    served statically, the salt silently becomes the only barrier and a
    six-character one. This fails then, which is the point: the premise is
    load-bearing, so it gets a tripwire instead of a paragraph."""
    src = (REPO / "api" / "routers" / "posts.py").read_text()
    i = src.index('@router.get("/post/{post_id}/content")')
    sig = src[i:src.index(":\n", src.index("async def get_post_content", i))]
    assert "get_current_user" not in sig, (
        "/api/post/{id}/content now requires a user. The salt was reclassified "
        "as a frozen naming input rather than a credential BECAUSE this endpoint "
        "was open (kura#43-2, #58). If the API is closed while /data is still "
        "served, revisit that decision before shipping this.")


# ── round 3: the sample is spread, not a prefix ────────────────────────────
#
# primary-codex: "先頭 20 件の照合は列挙順に偏り、salt が混在した部分移行・復旧を見逃す".
# Measured on primary before deciding: 1,038,713 entries enumerate in 0.33s, so
# counting first and then checking evenly spaced indices is affordable at boot.


def test_the_sample_reaches_both_ends_of_the_enumeration():
    """A prefix of twenty answers the configuration question but cannot see a
    tree holding files named under two salts, which is what review asked for.
    The spread always includes the first and last positions.

    Tested on the arithmetic rather than through a directory, deliberately. The
    first version of this test built the fixture by renaming files — which
    changes the enumeration order, so the ends it measured were not the ends it
    then checked. It failed, and it would have "proved" a correct implementation
    wrong."""
    from api.services.files import _spread_indices
    for total, sample in [(1000, 20), (50, 7), (3, 2), (1_038_713, 20)]:
        idx = _spread_indices(total, sample)
        assert min(idx) == 0 and max(idx) == total - 1
        assert len(idx) == sample
        gaps = sorted(sorted(idx)[i + 1] - sorted(idx)[i] for i in range(sample - 1))
        assert gaps[-1] - gaps[0] <= 1, "spacing is not even"


def test_the_spread_handles_the_edges_of_its_own_arithmetic():
    """It divides by (sample - 1) and indexes total - 1."""
    from api.services.files import _spread_indices
    assert _spread_indices(1, 20) == {0}
    assert _spread_indices(1000, 1) == {0}
    assert _spread_indices(0, 20) == {0}
    assert _spread_indices(5, 5000) == {0, 1, 2, 3, 4}


def test_a_full_sweep_checks_every_file(monkeypatch, tmp_path):
    """sample=None is what actually settles a mixed tree. A sample of twenty only
    makes a large mixed region likely to be noticed, and the docstring says so
    rather than claiming detection it does not have."""
    _plant(tmp_path, "correct", range(1, 101), wrong=set(range(91, 101)))
    files = _reload_files(monkeypatch, "correct", tmp_path)
    assert files.verify_salt_names_the_files(None) == ("mismatch", 100, 90)


def test_a_sample_larger_than_the_tree_checks_all_of_it(monkeypatch, tmp_path):
    _plant(tmp_path, "correct", range(1, 11))
    files = _reload_files(monkeypatch, "correct", tmp_path)
    assert files.verify_salt_names_the_files(5000) == ("ok", 10, 10)


def test_a_single_file_tree_still_verifies(monkeypatch, tmp_path):
    """The spacing arithmetic divides by (sample - 1); one file and one sample
    are both edges of it."""
    _plant(tmp_path, "correct", [7])
    files = _reload_files(monkeypatch, "correct", tmp_path)
    assert files.verify_salt_names_the_files(20) == ("ok", 1, 1)
    assert files.verify_salt_names_the_files(1) == ("ok", 1, 1)



# ── kura#61: the migration makes a two-salt tree the INTENDED state ─────────
#
# 2026-09-05, live on primary. The salt guard was written when "one salt names every
# file" was an invariant, and kura#61 Phase A deliberately breaks it: the
# backfill gives every file a second name derived by the transition salt. The
# first `systemctl restart kura-api` after the backfill refused to boot —
# "of 20 existing filenames sampled, 19 were derived by this salt" — and kura
# served nothing until the guard learned about phases. Nothing was wrong with
# the disk; the guard was asking a question the migration had retired.
#
# What still has to be true in a phase is narrower, and these fix that shape:
#   every name comes from one of the TWO salts in play (a third is still wrong)
#   at least one comes from the READER's (the salt the URLs are built with)


def _reload_files_in_phase(monkeypatch, salt, data_dir, *, next_salt=None, legacy=None):
    monkeypatch.setenv("KURA_FS_NAME_SALT_NEXT", next_salt or "")
    monkeypatch.setenv("KURA_FS_NAME_SALT_LEGACY", legacy or "")
    return _reload_files(monkeypatch, salt, data_dir)


def test_the_backfilled_second_name_is_not_a_wrong_salt(monkeypatch, tmp_path):
    """The live outage. Both names of the same file are on disk; the reader's
    salt is right; before this the boot guard called it a mismatch and refused."""
    _plant(tmp_path, "reader-salt", range(1, 6))
    _plant(tmp_path, "next-salt", range(1, 6))          # what backfill adds
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path, next_salt="next-salt")
    verdict, checked, matched = files.verify_salt_names_the_files(None)
    assert verdict == "ok"
    assert checked == 10 and matched == 5                # `matched` still counts the READER's

    # Phase B is the same shape with the legacy salt as the second one.
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path, legacy="next-salt")
    assert files.verify_salt_names_the_files(None)[0] == "ok"


def test_a_third_salt_is_still_a_wrong_salt_during_a_phase(monkeypatch, tmp_path):
    """Widening the guard must not turn it off: a name from neither salt in play
    is exactly the partial-restore / wrong-value case the guard exists for."""
    _plant(tmp_path, "reader-salt", range(1, 6))
    _plant(tmp_path, "next-salt", range(1, 6))
    _plant(tmp_path, "some-other-deployment", [7])
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path, next_salt="next-salt")
    assert files.verify_salt_names_the_files(None)[0] == "mismatch"


def test_a_tree_named_only_by_the_transition_salt_still_refuses(monkeypatch, tmp_path):
    """The salts swapped: consistent tree, and every URL 404s, because the URLs
    are built with the reader's salt and nothing on disk answers to it. Naming
    it separately from `mismatch` is the difference between 'someone restored
    the wrong tree' and 'the phase flip was done in the wrong order'."""
    _plant(tmp_path, "next-salt", range(1, 6))
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path, next_salt="next-salt")
    verdict, checked, matched = files.verify_salt_names_the_files(None)
    assert verdict == "reader-absent"
    assert checked == 5 and matched == 0


def test_outside_a_phase_the_second_name_is_still_a_mismatch(monkeypatch, tmp_path):
    """No phase set means one salt names everything. The widening is scoped to
    the phase; ending the phase with second names still on disk is a real
    defect and must not be silently accepted."""
    _plant(tmp_path, "reader-salt", range(1, 6))
    _plant(tmp_path, "next-salt", range(1, 6))
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path)
    assert files.verify_salt_names_the_files(None)[0] == "mismatch"


@pytest.mark.parametrize("verdict", ["mismatch", "reader-absent", "reader-missing"])
def test_the_boot_guard_refuses_every_verdict_that_would_404(monkeypatch, verdict):
    """RUN the guard under each verdict instead of reading its source.

    This replaced a test that sliced `api/main.py` as a string and asserted
    `'verdict == "reader-absent"' in guard` plus `count("raise RuntimeError") >= 4`
    (kura#61 review round 1, both the requester and the reviewing seat). That shape
    passes while the branch is unreachable, passes if the raises belong to unrelated
    checks, and has to be re-counted by hand every time a verdict is added — it was
    already at `>= 4` and climbing. kura#61's own plan (rev 3) retired exactly this
    proxy elsewhere in favour of AST; behaviour is better still, because the question
    here is not "is the branch written" but "does the process refuse to boot".
    """
    import api.main as main
    monkeypatch.setattr("api.config.FS_NAME_SALT", "some-salt", raising=False)
    monkeypatch.setattr(main, "FS_NAME_SALT", "some-salt", raising=False)
    monkeypatch.setattr("api.services.files.verify_salt_names_the_files",
                        lambda *a, **k: (verdict, 20, 1))
    with pytest.raises(RuntimeError) as e:
        main._check_fs_name_salt()
    # and the refusal has to say WHICH tree it looked at — a bare "refused" sends the
    # operator to the environment, which is the one place that cannot answer (kura#43-2).
    assert "KURA_FS_NAME_SALT" in str(e.value)


@pytest.mark.parametrize("verdict", ["ok", "no-files"])
def test_the_boot_guard_lets_a_serving_tree_start(monkeypatch, verdict):
    """The other half: a guard that refuses everything would pass the test above and
    still be broken. `no-files` is not a pass, but it is not a refusal either — a node
    with no local media boots (the salt is unverifiable there, and saying so is the
    caller's job, not the guard's)."""
    import api.main as main
    # A node with files keeps whatever salt named them; one with none yet needs a generated
    # salt (kura#61, the next test), so the no-files case here is given one.
    salt = "some-salt" if verdict == "ok" else "x" * main.MIN_NEW_SALT
    monkeypatch.setattr("api.config.FS_NAME_SALT", salt, raising=False)
    monkeypatch.setattr(main, "FS_NAME_SALT", salt, raising=False)
    monkeypatch.setattr("api.services.files.verify_salt_names_the_files",
                        lambda *a, **k: (verdict, 0 if verdict == "no-files" else 20, 20))
    main._check_fs_name_salt()          # must not raise


@pytest.mark.parametrize("media,refused", [("", True), ("expected", True), ("none", False)])
def test_a_short_salt_with_no_file_named_yet_refuses_unless_the_node_holds_no_media(monkeypatch, media, refused):
    """kura#61's product side: nothing ties a node with no named file to an old value, and a
    short salt is brute-forced from one published filename — so a new install must generate
    one. A replica (KURA_FS_MEDIA=none) names no file and keeps its value."""
    import api.main as main
    monkeypatch.setattr(main, "FS_NAME_SALT", "short", raising=False)
    monkeypatch.setattr(main, "FS_MEDIA", media, raising=False)
    monkeypatch.setattr("api.services.files.verify_salt_names_the_files", lambda *a, **k: ("no-files", 0, 0))
    if refused:
        with pytest.raises(RuntimeError, match="make_env.py"):
            main._check_fs_name_salt()
    else:
        main._check_fs_name_salt()


def test_a_file_named_by_the_transition_salt_only_refuses_the_boot(monkeypatch, tmp_path):
    """Finding B (2026-09-05): in Phase B the guard of 1.199.4 booted a tree where
    728,871 of 1,032,375 posts had no reader-salt name — consistent, and 70.6% 404.
    A secondary-derived name without its reader-derived sibling is `reader-missing`."""
    _plant(tmp_path, "new-salt", range(1, 4))           # migrated: reader (new) names
    _plant(tmp_path, "old-salt", range(1, 4))           # ... and their legacy names
    _plant(tmp_path, "old-salt", range(4, 9))           # NOT migrated: legacy name only
    files = _reload_files_in_phase(monkeypatch, "new-salt", tmp_path, legacy="old-salt")
    verdict, checked, matched = files.verify_salt_names_the_files(None)
    assert verdict == "reader-missing" and checked == 11 and matched == 3
    _plant(tmp_path, "new-salt", range(4, 9))           # the backfill reaches them
    assert files.verify_salt_names_the_files(None)[0] == "ok"
    # Phase A, the same shape: a NEXT-derived name without a reader-derived one is wrong too
    files = _reload_files_in_phase(monkeypatch, "old-salt", tmp_path, next_salt="new-salt")
    assert files.verify_salt_names_the_files(None)[0] == "ok"
    _plant(tmp_path, "new-salt", [42])
    assert files.verify_salt_names_the_files(None)[0] == "reader-missing"


def test_a_phase_b_backfill_that_never_ran_is_reader_missing_not_a_swap(monkeypatch, tmp_path):
    """FINDING C (kura#61 review round 1, seat 2). The 0%-done Phase B flip is the
    SAME defect as the 50%-done one, at 100% — and it was the one shape that could
    not reach `reader-missing`, because `matched == 0` was tested first and claimed
    the salts were swapped. The two refusals prescribe opposite operator actions:
    `reader-absent` sends you to the environment, which on a merely unmigrated tree
    is the wrong turn kura#43-2 exists to prevent (the env cannot answer whether its
    value is right; the disk can, and here the disk says 'not backfilled yet')."""
    _plant(tmp_path, "old-salt", range(1, 9))            # legacy names only: backfill 0%
    files = _reload_files_in_phase(monkeypatch, "new-salt", tmp_path, legacy="old-salt")
    for sample in (None, 20, 1):                          # boot default AND full sweep
        assert files.verify_salt_names_the_files(sample)[0] == "reader-missing"
    # Phase A keeps `reader-absent`: its backfill only ADDS the _NEXT name, so a tree
    # with no reader-derived name was never named by this reader. An unfinished
    # backfill cannot produce that shape there, which is what makes the phase the
    # discriminator — the disk alone cannot tell a 0% Phase B from a swap.
    files = _reload_files_in_phase(monkeypatch, "reader-salt", tmp_path, next_salt="old-salt")
    assert files.verify_salt_names_the_files(None)[0] == "reader-absent"


def test_the_reader_missing_refusal_says_to_check_for_a_swap_when_nothing_matched(monkeypatch):
    """Because the disk cannot distinguish 0%-backfilled from swapped, the refusal
    has to name both — and put the cheap check first. Running a backfill under a
    swapped salt writes new names under the WRONG salt, which is unrecoverable by
    running it again."""
    import api.main as main
    monkeypatch.setattr("api.config.FS_NAME_SALT", "some-salt", raising=False)
    monkeypatch.setattr(main, "FS_NAME_SALT", "some-salt", raising=False)

    monkeypatch.setattr("api.services.files.verify_salt_names_the_files",
                        lambda *a, **k: ("reader-missing", 20, 0))
    with pytest.raises(RuntimeError) as nothing:
        main._check_fs_name_salt()
    assert "BEFORE running the backfill" in str(nothing.value)
    assert "KURA_FS_NAME_SALT_LEGACY" in str(nothing.value)

    # A partially backfilled tree HAS proof the reader's salt names files, so the
    # swap clause would be noise there — finish the backfill is the whole answer.
    monkeypatch.setattr("api.services.files.verify_salt_names_the_files",
                        lambda *a, **k: ("reader-missing", 20, 7))
    with pytest.raises(RuntimeError) as partial:
        main._check_fs_name_salt()
    assert "BEFORE running the backfill" not in str(partial.value)
    assert "Finish the backfill" in str(partial.value)


def _plant_tree(data_dir, subdir, salt, ids, ext="jpg"):
    """_plant, but for any of the salt-named trees rather than posts/ alone."""
    import hmac
    tree = data_dir / subdir
    tree.mkdir(parents=True, exist_ok=True)
    for pid in ids:
        h = hmac.new(salt.encode(), str(pid).encode(), digestmod="md5").hexdigest()[:16]
        (tree / f"{pid}_{h}.{ext}").write_bytes(b"x")
    return tree


@pytest.mark.parametrize("missed,ext", [("generated-thumbnails", "jpg"),
                                        ("posts/custom-thumbnails", "dat")])
def test_a_backfill_that_skipped_a_thumbnail_tree_refuses_the_boot(monkeypatch, tmp_path,
                                                                   missed, ext):
    """FINDING A3 (kura#61 review round 1, raised by the requester, unfixed until now).
    The guard read DATA_DIR/'posts' and nothing else, but generated-thumbnails/ and
    posts/custom-thumbnails/ are salt-named through the same `_KINDS` and the kura#61
    migration doubles them too. A backfill that reached posts/ and not a thumbnail
    tree therefore booted 'ok' and 404'd every thumbnail in it — silently, which is
    the exact condition the guard exists to refuse.

    Parametrized over the trees rather than fixed to one, because the failure was
    'there is more than one of these', not 'this one was missed'."""
    ids = range(1, 30)
    _plant_tree(tmp_path, "posts", "new-salt", ids)         # content: backfill done
    _plant_tree(tmp_path, "posts", "old-salt", ids)
    _plant_tree(tmp_path, missed, "old-salt", ids, ext)     # this tree: legacy name only
    files = _reload_files_in_phase(monkeypatch, "new-salt", tmp_path, legacy="old-salt")
    assert files.verify_salt_names_the_files(None)[0] == "reader-missing"
    assert files.verify_salt_names_the_files(20)[0] == "reader-missing"
    _plant_tree(tmp_path, missed, "new-salt", ids, ext)     # the backfill reaches it
    assert files.verify_salt_names_the_files(None)[0] == "ok"


def test_every_salt_named_tree_is_swept_exactly_once(monkeypatch, tmp_path):
    """The trees come from `_KINDS`, so a new salt-named kind is covered without
    editing a second list. custom-thumbnails lives UNDER posts/ and scandir does
    not recurse, so the count is a sum, not a double count — pinned as a number
    because 'it looked right' is how the nested tree got missed in the first place."""
    ids = range(1, 30)
    for subdir, ext in (("posts", "jpg"), ("generated-thumbnails", "jpg"),
                        ("posts/custom-thumbnails", "dat")):
        _plant_tree(tmp_path, subdir, "new-salt", ids, ext)
        _plant_tree(tmp_path, subdir, "old-salt", ids, ext)
    files = _reload_files_in_phase(monkeypatch, "new-salt", tmp_path, legacy="old-salt")
    verdict, checked, matched = files.verify_salt_names_the_files(None)
    assert verdict == "ok"
    assert checked == 29 * 2 * 3 and matched == 29 * 3


def test_a_node_with_no_thumbnails_yet_is_not_a_finding(monkeypatch, tmp_path):
    """Absence of a thumbnail tree is a normal shape — nothing there to check the
    salt against. Only the CONTENT tree's absence means the volume is not mounted,
    and that keeps its own verdict."""
    _plant_tree(tmp_path, "posts", "only-salt", range(1, 30))
    files = _reload_files(monkeypatch, "only-salt", tmp_path)
    assert files.verify_salt_names_the_files(None) == ("ok", 29, 29)


def test_a_backfilled_tree_is_not_refused_because_the_sample_missed_the_reader(monkeypatch, tmp_path):
    """Also round 1, seat 2. `matched == 0` was read as 'the reader's salt names
    nothing here', but it is a statement about the SAMPLE. On a fully backfilled
    tree every post carries two names, so a sample that lands only on the second
    ones has matched == 0 while the reader's names are right there on disk —
    measured 200/200 refusals at sample=1 before this. The sibling stat() already
    proves presence, so no-orphans is the answer regardless of what was sampled."""
    _plant(tmp_path, "new-salt", range(1, 40))          # reader names
    _plant(tmp_path, "old-salt", range(1, 40))          # ... and their legacy names
    files = _reload_files_in_phase(monkeypatch, "new-salt", tmp_path, legacy="old-salt")
    assert files.verify_salt_names_the_files(None)[0] == "ok"
    for _ in range(20):                                  # sample=1 lands wherever it lands
        assert files.verify_salt_names_the_files(1)[0] == "ok"


def test_naming_guard_checks_offline_alias_names_without_resolving_targets(monkeypatch, tmp_path):
    posts = _plant(tmp_path, 'reader-salt', range(1, 6))
    for path in posts.iterdir():
        path.unlink()
        path.symlink_to(tmp_path / 'offline-disk' / path.name)
    files = _reload_files(monkeypatch, 'reader-salt', tmp_path)
    real_scandir = files.os.scandir
    class Entry:
        def __init__(self, value):self.value=value;self.name=value.name
        def is_file(self, *, follow_symlinks=True):
            assert follow_symlinks is False, 'Naming guard must not access archive targets'
            return self.value.is_file(follow_symlinks=False)
        def is_symlink(self):return self.value.is_symlink()
    class Scan:
        def __init__(self, path):self.iterator=real_scandir(path)
        def __enter__(self):return (Entry(value) for value in self.iterator)
        def __exit__(self, *args):self.iterator.close()
    monkeypatch.setattr(files.os, 'scandir', Scan)
    assert files.verify_salt_names_the_files() == ('ok', 5, 5)
    assert files.verify_salt_names_the_files(None) == ('ok', 5, 5)
    # Even a broken alias under a third salt must not disappear from the audit.
    (posts/'9_0000000000000000.jpg').symlink_to(tmp_path/'offline-disk'/'missing')
    assert files.verify_salt_names_the_files(None)[0] == 'mismatch'


def test_offline_transition_aliases_still_require_reader_sibling(monkeypatch, tmp_path):
    import hmac
    posts = tmp_path/'posts';posts.mkdir()
    def alias(salt):
        value=hmac.new(salt.encode(),b'1',digestmod='md5').hexdigest()[:16]
        return posts/f'1_{value}.jpg'
    alias('legacy-salt').symlink_to(tmp_path/'offline-disk'/'object')
    files = _reload_files_in_phase(monkeypatch,'reader-salt',tmp_path,legacy='legacy-salt')
    assert files.verify_salt_names_the_files(None)[0] == 'reader-missing'
    alias('reader-salt').symlink_to(tmp_path/'offline-disk'/'object')
    assert files.verify_salt_names_the_files(None) == ('ok',2,1)
