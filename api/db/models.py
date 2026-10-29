"""
SQLAlchemy ORM models for the kura database.

No DDL is emitted on startup — tables are managed via alembic (api/alembic/versions/).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY as PgARRAY
from sqlalchemy.orm import DeclarativeBase, deferred, relationship


class Base(DeclarativeBase):
    pass


# ── Association / junction tables ──────────────────────────────────────

class PostTag(Base):
    __tablename__ = "post_tag"

    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), primary_key=True)
    tag_id = sa.Column("tag_id", sa.Integer, sa.ForeignKey("tag.id"), primary_key=True)
    locked = sa.Column("locked", sa.Boolean, nullable=False, server_default=sa.text("false"))


class TagSuggestion(Base):
    __tablename__ = "tag_suggestion"

    parent_id = sa.Column("parent_id", sa.Integer, sa.ForeignKey("tag.id"), primary_key=True)
    child_id = sa.Column("child_id", sa.Integer, sa.ForeignKey("tag.id"), primary_key=True)


class TagImplication(Base):
    __tablename__ = "tag_implication"

    parent_id = sa.Column("parent_id", sa.Integer, sa.ForeignKey("tag.id"), primary_key=True)
    child_id = sa.Column("child_id", sa.Integer, sa.ForeignKey("tag.id"), primary_key=True)


# ── Tag ────────────────────────────────────────────────────────────────

class TagCategory(Base):
    __tablename__ = "tag_category"

    id = sa.Column("id", sa.Integer, primary_key=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    name = sa.Column("name", sa.Unicode(32), nullable=False)
    color = sa.Column("color", sa.Unicode(32), nullable=False, default="#000000")
    default = sa.Column("default", sa.Boolean, nullable=False, default=False)
    order = sa.Column("order", sa.Integer, nullable=False, default=1)


class TagName(Base):
    __tablename__ = "tag_name"

    tag_name_id = sa.Column("tag_name_id", sa.Integer, primary_key=True)
    tag_id = sa.Column("tag_id", sa.Integer, sa.ForeignKey("tag.id"), nullable=False, index=True)
    name = sa.Column("name", sa.Unicode(128), nullable=False, unique=True)
    order = sa.Column("ord", sa.Integer, nullable=False, index=True)


class TagLocalization(Base):
    __tablename__ = "tag_localization"
    tag_id = sa.Column(sa.Integer, sa.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)
    locale = sa.Column(sa.String(35), primary_key=True)
    name = sa.Column(sa.Unicode(128), nullable=False)


class Tag(Base):
    __tablename__ = "tag"

    tag_id = sa.Column("id", sa.Integer, primary_key=True)
    category_id = sa.Column("category_id", sa.Integer, sa.ForeignKey("tag_category.id"), nullable=False, index=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_edit_time = sa.Column("last_edit_time", sa.DateTime)
    description = sa.Column("description", sa.UnicodeText, default=None)

    # Physical column in the patched szurubooru schema (tuning doc #4).
    # Falls back to computed if the physical column doesn't exist yet.
    post_count = sa.Column("post_count", sa.Integer, server_default="0")

    category = relationship("TagCategory", lazy="joined")
    localizations = relationship("TagLocalization", cascade="all,delete-orphan", lazy="selectin")
    names = relationship("TagName", cascade="all,delete-orphan", lazy="joined", order_by="TagName.order")
    suggestions = relationship(
        "Tag",
        secondary="tag_suggestion",
        primaryjoin=lambda: Tag.tag_id == TagSuggestion.parent_id,
        secondaryjoin=lambda: Tag.tag_id == TagSuggestion.child_id,
        lazy="select",
    )
    implications = relationship(
        "Tag",
        secondary="tag_implication",
        primaryjoin=lambda: Tag.tag_id == TagImplication.parent_id,
        secondaryjoin=lambda: Tag.tag_id == TagImplication.child_id,
        lazy="select",
    )
    links = relationship(
        "TagLink", cascade="all,delete-orphan", lazy="select",
        order_by="TagLink.platform",
    )


class TagRelation(Base):
    """Symmetric typed relation between two tags (migration 0064). Normalized
    a_tag_id < b_tag_id, one row per pair; kind: 'related' (conceptual
    neighbours, e.g. phimosis-foreskin) | 'exclusive' (mutually exclusive
    alternatives PER SUBJECT, e.g. the penis-size family, stored as pairwise
    closure — multi-character/multi-part cuts can legitimately carry both, so
    co-occurrence is not mechanically cleanable)."""

    __tablename__ = "tag_relation"

    a_tag_id = sa.Column("a_tag_id", sa.Integer, sa.ForeignKey("tag.id", ondelete="CASCADE"),
                         primary_key=True)
    b_tag_id = sa.Column("b_tag_id", sa.Integer, sa.ForeignKey("tag.id", ondelete="CASCADE"),
                         primary_key=True)
    kind = sa.Column("kind", sa.Text, nullable=False)
    note = sa.Column("note", sa.Text)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False,
                              server_default=sa.func.now())


class TagLink(Base):
    """An external account/link for a tag (artist tags: accounts on external
    platforms, shops). `uncensored` is tri-state (None=unknown, 0=censored,
    1=uncensored); `verified` marks a sample-confirmed classification vs a
    platform-implied guess. See migration 0060."""

    __tablename__ = "tag_link"

    id = sa.Column("id", sa.BigInteger, primary_key=True)
    tag_id = sa.Column("tag_id", sa.Integer, sa.ForeignKey("tag.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    platform = sa.Column("platform", sa.Text, nullable=False, index=True)
    url = sa.Column("url", sa.Text, nullable=False)
    handle = sa.Column("handle", sa.Text)
    # The kura pool this external account resolves to (its imported collection).
    # Hub that links the account to an extension's source record (same pool_id)
    # for its tracking state. Nullable: an account may have no pool yet.
    # See migration 0063.
    pool_id = sa.Column("pool_id", sa.Integer,
                        sa.ForeignKey("pool.id", ondelete="SET NULL"), index=True)
    uncensored = sa.Column("uncensored", sa.SmallInteger)  # None | 0 | 1
    verified = sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.text("false"))
    note = sa.Column("note", sa.Text)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True),
                           nullable=False, server_default=sa.text("now()"))
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True),
                           nullable=False, server_default=sa.text("now()"))

    __table_args__ = (
        sa.UniqueConstraint("tag_id", "url", name="uq_tag_link_tag_url"),
    )


# ── Pool ───────────────────────────────────────────────────────────────

class PoolCategory(Base):
    __tablename__ = "pool_category"

    id = sa.Column("id", sa.Integer, primary_key=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    name = sa.Column("name", sa.Unicode(32), nullable=False)
    color = sa.Column("color", sa.Unicode(32), nullable=False, default="#000000")
    default = sa.Column("default", sa.Boolean, nullable=False, default=False)


class PoolName(Base):
    __tablename__ = "pool_name"

    pool_name_id = sa.Column("pool_name_id", sa.Integer, primary_key=True)
    pool_id = sa.Column("pool_id", sa.Integer, sa.ForeignKey("pool.id"), nullable=False, index=True)
    name = sa.Column("name", sa.Unicode(128), nullable=False, unique=True)
    order = sa.Column("ord", sa.Integer, nullable=False, index=True)


class PoolPost(Base):
    __tablename__ = "pool_post"

    pool_id = sa.Column("pool_id", sa.Integer, sa.ForeignKey("pool.id"), primary_key=True)
    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), primary_key=True)
    order = sa.Column("ord", sa.Integer, nullable=False, index=True)

    pool = relationship("Pool", back_populates="_posts")
    post = relationship("Post", back_populates="_pools", lazy="joined")


class Pool(Base):
    __tablename__ = "pool"

    pool_id = sa.Column("id", sa.Integer, primary_key=True)
    category_id = sa.Column("category_id", sa.Integer, sa.ForeignKey("pool_category.id"), nullable=False, index=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_edit_time = sa.Column("last_edit_time", sa.DateTime)
    description = sa.Column("description", sa.UnicodeText, default=None)

    # 検証 pool ライフサイクル (migration 0058): NULL=通常 / 'pending'=検証待ち / 'verified'=検証済み。
    # tagger_source = 生成した head 世代 (trainer 自動更新ループの provenance、UI 表示用)。
    verify_status = sa.Column("verify_status", sa.Text, nullable=True)
    tagger_source = sa.Column("tagger_source", sa.Text, nullable=True)

    meta_tagging_status = sa.Column("meta_tagging_status", sa.Text, nullable=False, default="pending")
    meta_tagging_locked_until = sa.Column("meta_tagging_locked_until", sa.DateTime(timezone=True))
    meta_tagging_attempts = sa.Column("meta_tagging_attempts", sa.Integer, nullable=False, default=0)
    meta_tagging_last_error = sa.Column("meta_tagging_last_error", sa.Text)

    # cover_post_id: first post by ord, trigger-maintained (migration 0013).
    cover_post_id = sa.Column(
        "cover_post_id", sa.Integer,
        sa.ForeignKey("post.id", ondelete="SET NULL"),
        nullable=True,
    )

    category = relationship("PoolCategory", lazy="joined")
    names = relationship("PoolName", cascade="all,delete-orphan", lazy="joined", order_by="PoolName.order")
    _posts = relationship(
        "PoolPost",
        cascade="all,delete-orphan",
        lazy="select",
        back_populates="pool",
        order_by="PoolPost.order",
    )
    state = relationship("PoolState", uselist=False, back_populates="pool",
                         cascade="all,delete-orphan", lazy="joined")
    urls = relationship(
        "PoolUrl", cascade="all,delete-orphan",
        lazy="select", back_populates="pool",
    )

    @property
    def primary_url(self) -> "PoolUrl | None":
        return next((u for u in self.urls if u.is_primary), None)


class PoolUrl(Base):
    """A pool's source URLs (Phase C / migration 0013): one primary, the rest aliases.

    Replaces pool.primary_source_url + pool_url_alias. expected_count is the
    size the pool should reach; observed_count is its member count, kept by a
    trigger. Extensions keep their own per-URL state in columns this class does
    not map.
    """

    __tablename__ = "pool_url"

    pool_id = sa.Column(
        "pool_id", sa.Integer,
        sa.ForeignKey("pool.id", ondelete="CASCADE"),
        nullable=False,
    )
    url = sa.Column("url", sa.Text, nullable=False, unique=True)
    is_primary = sa.Column("is_primary", sa.Boolean, nullable=False, default=False)
    created_at = sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )
    expected_count = sa.Column("expected_count", sa.Integer)
    observed_count = sa.Column("observed_count", sa.Integer)
    last_observed_at = sa.Column("last_observed_at", sa.DateTime(timezone=True))

    __table_args__ = (
        sa.PrimaryKeyConstraint("pool_id", "url", name="pool_url_pkey"),
    )

    pool = relationship("Pool", back_populates="urls", lazy="joined")


class PoolState(Base):
    """A pool's scope (post | creator | collection), 1:1 with Pool.

    Extensions keep their own per-pool state in columns this class does not map.
    """
    __tablename__ = "pool_state"

    pool_id = sa.Column("pool_id", sa.Integer,
                        sa.ForeignKey("pool.id", ondelete="CASCADE"),
                        primary_key=True)
    scope = sa.Column("scope", sa.Text, nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True),
                           nullable=False,
                           default=lambda: __import__("datetime").datetime.utcnow())
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True),
                           nullable=False,
                           default=lambda: __import__("datetime").datetime.utcnow())

    pool = relationship("Pool", back_populates="state", lazy="joined")


# ── User ───────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "user"

    user_id = sa.Column("id", sa.Integer, primary_key=True)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_login_time = sa.Column("last_login_time", sa.DateTime)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    name = sa.Column("name", sa.Unicode(50), nullable=False, unique=True)
    # Nullable since 0024: broker-federated users never set a local password.
    password_hash = sa.Column("password_hash", sa.Unicode(128), nullable=True)
    password_salt = sa.Column("password_salt", sa.Unicode(32))
    password_revision = sa.Column("password_revision", sa.SmallInteger, default=0, nullable=False)
    email = sa.Column("email", sa.Unicode(64), nullable=True)
    rank = sa.Column("rank", sa.Unicode(32), nullable=False)
    avatar_style = sa.Column("avatar_style", sa.Unicode(32), nullable=False, default="gravatar")
    # Broker trust tier the account was provisioned with (federated users).
    broker_trust_tier = sa.Column("broker_trust_tier", sa.Unicode(32), nullable=True)


class UserToken(Base):
    __tablename__ = "user_token"

    id = sa.Column("id", sa.Integer, primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    token = sa.Column("token", sa.Unicode(36), nullable=False)
    note = sa.Column("note", sa.Unicode(128), nullable=True)
    enabled = sa.Column("enabled", sa.Boolean, nullable=False, default=True)
    expiration_time = sa.Column("expiration_time", sa.DateTime, nullable=True)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_edit_time = sa.Column("last_edit_time", sa.DateTime)
    last_usage_time = sa.Column("last_usage_time", sa.DateTime)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)

    user = relationship("User", lazy="joined")


# ── Post ───────────────────────────────────────────────────────────────

class PostFeature(Base):
    __tablename__ = "post_feature"

    id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), nullable=False, index=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    time = sa.Column("time", sa.DateTime, nullable=False)


class PostScore(Base):
    __tablename__ = "post_score"

    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True)
    time = sa.Column("time", sa.DateTime, nullable=False)
    score = sa.Column("score", sa.Integer, nullable=False)


class PostFavorite(Base):
    __tablename__ = "post_favorite"

    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True)
    time = sa.Column("time", sa.DateTime, nullable=False)


class PostNote(Base):
    __tablename__ = "post_note"

    id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), nullable=False, index=True)
    polygon = sa.Column("polygon", sa.PickleType, nullable=False)
    text = sa.Column("text", sa.UnicodeText, nullable=False)


# PostSignature (legacy szurubooru perceptual signature) dropped in migration
# 0035 (Phase 6) — superseded by post.perceptual_hash / perceptual_hash_bands.


class Post(Base):
    __tablename__ = "post"

    SAFETY_SAFE = "safe"
    SAFETY_SKETCHY = "sketchy"
    SAFETY_UNSAFE = "unsafe"

    TYPE_IMAGE = "image"
    TYPE_ANIMATION = "animation"
    TYPE_VIDEO = "video"
    TYPE_FLASH = "flash"
    TYPE_DOCUMENT = "document"

    # Post types the auto-tagger can process (images, gifs, and videos via
    # first-frame extraction). Documents (PDF) and flash are never queued:
    # the claim query filters them out, and the AFTER INSERT trigger on post
    # gives them a 'skipped' work row instead of 'pending'. Both repeat this
    # tuple as a SQL literal (kura_reconciler's functions cannot import it);
    # tests/test_tagging_work_stage4a.py ties the trigger's copy to it.
    TAGGABLE_TYPES = (TYPE_IMAGE, TYPE_ANIMATION, TYPE_VIDEO)

    post_id = sa.Column("id", sa.Integer, primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_edit_time = sa.Column("last_edit_time", sa.DateTime)
    safety = sa.Column("safety", sa.Unicode(32), nullable=False)
    source = sa.Column("source", sa.Unicode(2048))
    original_filename = sa.Column("original_filename", sa.Text, nullable=True)
    flags_string = sa.Column("flags", sa.Unicode(32), default="")

    type = sa.Column("type", sa.Unicode(32), nullable=False)
    checksum = sa.Column("checksum", sa.Unicode(64), nullable=False)
    checksum_md5 = sa.Column("checksum_md5", sa.Unicode(32))
    file_size = sa.Column("file_size", sa.BigInteger)
    canvas_width = sa.Column("image_width", sa.Integer)
    canvas_height = sa.Column("image_height", sa.Integer)
    mime_type = sa.Column("mime-type", sa.Unicode(32), nullable=False)

    # The tagging queue is post_tagging_work (one row per post, written by the
    # trigger on INSERT and by the governed functions), read through
    # api/services/tagging_queue.py — docs/notes/tagging-work-table-plan.md.
    # Tagging provenance (task 4328fb0e) lives in post_tag_audit (migration
    # 0054), not as columns here -- migration 0053's flat snapshot columns
    # were replaced after adversarial review found they silently discarded
    # history on re-tagging. No ORM model for post_tag_audit; written via
    # raw SQL in api/services/tagging.py, matching the tagger_registry
    # tables' (migration 0052) existing no-ORM precedent.

    # ── pg-native media (migrations 0025-0029; see COMMENT ON COLUMN in DB) ──
    # data is WebP (cap1600/q82) for status='ok' images (media_location='pg');
    # videos keep data NULL (media_location='hdd', body streamed from HDD).
    # deferred(): the image/thumbnail blobs are NEVER pulled by select(Post). A
    # list/grid fetch (e.g. /api/posts?limit=40) would otherwise SELECT * and
    # materialise 40 full-size WebP bodies per request into the worker — under
    # virtual-scroll fan-out that balloons RSS past the cgroup MemoryHigh and
    # throttle-stalls every request. Blobs load only via an explicit
    # select(Post.data)/select(Post.thumbnail) in the content/thumbnail routes.
    data = deferred(sa.Column("data", sa.LargeBinary, nullable=True))
    thumbnail = deferred(sa.Column("thumbnail", sa.LargeBinary, nullable=True))  # WebP ~320px
    sha256 = sa.Column("sha256", sa.LargeBinary, nullable=True)        # original-source identity
    perceptual_hash = sa.Column("perceptual_hash", sa.LargeBinary, nullable=True)  # 64-bit dHash
    perceptual_hash_bands = sa.Column("perceptual_hash_bands", PgARRAY(sa.Integer), nullable=True)
    perceptual_distance = sa.Column("perceptual_distance", sa.SmallInteger, nullable=True)
    duplicate_of = sa.Column("duplicate_of", sa.Integer,
                             sa.ForeignKey("post.id", ondelete="RESTRICT"), nullable=True)
    status = sa.Column("status", sa.Text, nullable=True)  # pending|ok|exact_dup|near_dup|corrupt|lost
    media_location = sa.Column("media_location", sa.Text, nullable=True)  # pg|hdd
    # Tag membership lives only in post_tag (0179 retired the name arrays).

    # primary pool — schema-level "where this post came from at import time".
    # Nullable for legacy posts; new uploads with `collection_url` set this and
    # a trigger ensures the matching pool_post row exists (Phase A invariant).
    primary_pool_id = sa.Column(
        "primary_pool_id", sa.Integer,
        sa.ForeignKey("pool.id", ondelete="SET NULL"),
        nullable=True,
    )

    user = relationship("User", lazy="joined")
    tags = relationship("Tag", backref="posts", secondary="post_tag", lazy="select")
    scores = relationship("PostScore", cascade="all, delete-orphan", lazy="select")
    favorited_by = relationship("PostFavorite", cascade="all, delete-orphan", lazy="select")
    features = relationship("PostFeature", cascade="all, delete-orphan", lazy="select")
    notes = relationship("PostNote", cascade="all, delete-orphan", lazy="select")
    comments = relationship("Comment", cascade="all, delete-orphan", lazy="select")
    _pools = relationship("PoolPost", cascade="all,delete-orphan", lazy="select", order_by="PoolPost.order", back_populates="post")

    @property
    def flags(self) -> list[str]:
        return sorted([x for x in (self.flags_string or "").split(",") if x])

    @flags.setter
    def flags(self, data: list[str]) -> None:
        self.flags_string = ",".join([x for x in data if x])


# ── Censor annotation (issue #2 — human-only minimal slice) ─────────────
# Two single-role tables (no proposal/GT conflation): post-level review state
# and human-drawn observations. Each row is BOUND to the image version it was
# made on (image_sha256 + post_version_seen) so it can go `stale` when a post's
# image is replaced. geometry is normalized [0,1] of the served canonical image
# (post.data WebP, canvas_width×canvas_height); `mask` is the immutable raster
# snapshot used for processing/reproduction (geometry is the editable form).

class PostCensorReview(Base):
    __tablename__ = "post_censor_review"

    id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer,
                        sa.ForeignKey("post.id", ondelete="CASCADE"),
                        nullable=False, unique=True, index=True)
    image_sha256 = sa.Column("image_sha256", sa.LargeBinary, nullable=True)
    post_version_seen = sa.Column("post_version_seen", sa.Integer, nullable=True)
    # 'partial' | 'complete'  (absence of a row == 'unreviewed')
    state = sa.Column("state", sa.Text, nullable=False, default="partial")
    # explicit "looked, it's clean" — the training-negative signal
    has_no_censor = sa.Column("has_no_censor", sa.Boolean, nullable=False, default=False)
    # teacher-selection: when true, export_human_labels.py skips this post (its
    # auto-imported regions were judged bad enough to drop from the training set).
    teacher_excluded = sa.Column("teacher_excluded", sa.Boolean, nullable=False,
                                 server_default=sa.text("false"), default=False)
    reviewed_by = sa.Column("reviewed_by", sa.Integer,
                           sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = sa.Column("reviewed_at", sa.DateTime(timezone=True))


class CensorAnnotation(Base):
    __tablename__ = "censor_annotation"

    id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer,
                        sa.ForeignKey("post.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # image-version binding (staleness when post.sha256/version changes)
    image_sha256 = sa.Column("image_sha256", sa.LargeBinary, nullable=True)
    post_version_seen = sa.Column("post_version_seen", sa.Integer, nullable=True)
    # vocabularies validated in the app (Text, like post.safety/status) so new
    # censor/shape kinds don't need a migration:
    #   censor_type: mosaic|white_bar|black_bar|slash|other
    #   shape_type:  rect|circle|ellipse|polygon|pen_path
    censor_type = sa.Column("censor_type", sa.Text, nullable=False)
    shape_type = sa.Column("shape_type", sa.Text, nullable=False)
    # eraser: a negative region SUBTRACTS from the unioned mask. Consumers composite
    # final = union(positives) AND NOT union(negatives); lets a reviewer trim over-coverage.
    negative = sa.Column("negative", sa.Boolean, nullable=False, server_default=sa.text("false"))
    # normalized [0,1] geometry in canonical content space
    geometry = sa.Column("geometry", postgresql.JSONB, nullable=False)
    # immutable raster mask (PNG, canonical dims) — deferred so list queries
    # never materialise the blob (same rationale as Post.data/thumbnail).
    mask = deferred(sa.Column("mask", sa.LargeBinary, nullable=True))
    created_by = sa.Column("created_by", sa.Integer,
                          sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True))
    # optimistic-lock / ETag — INDEPENDENT of post.version (tag edits must not
    # collide with annotation edits).
    region_version = sa.Column("region_version", sa.Integer, nullable=False, default=1)
    # idempotency: a re-sent POST with the same uuid returns the same row.
    client_mutation_uuid = sa.Column("client_mutation_uuid",
                                     postgresql.UUID(as_uuid=True), nullable=True, unique=True)
    # soft-delete tombstone (one device can't resurrect another's delete).
    deleted_at = sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    supersedes_id = sa.Column("supersedes_id", sa.Integer,
                             sa.ForeignKey("censor_annotation.id", ondelete="SET NULL"),
                             nullable=True)


# ── Post variant (issue #2 — non-destructive AI reconstruction) ─────────
# An ALTERNATE rendering of a post's image (e.g. an AI "decensored" pass) kept
# in its OWN row with its OWN blobs. The original Post.data/thumbnail/sha256 are
# never touched. A variant is deliberately NOT a Post and NOT a post_relation:
# search/dedup/post-count/pool/auto-tag/favorite all keep targeting the original.
# Honesty (design rev2 §3): kind is `ai_reconstruction`, not "decensored" — the
# pixels are fabricated, not recovered. review_state gates the viewer: only an
# 'approved' variant is offered as a toggle. source_sha256/source_version_seen
# bind to the original image version so a later replace flags the variant stale.

class PostVariant(Base):
    __tablename__ = "post_variant"

    id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer,
                        sa.ForeignKey("post.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # vocab validated in the app (Text, like post.status): 'ai_reconstruction'
    kind = sa.Column("kind", sa.Text, nullable=False)
    # version binding to the ORIGINAL (staleness when post.sha256/version changes)
    source_sha256 = sa.Column("source_sha256", sa.LargeBinary, nullable=True)
    source_version_seen = sa.Column("source_version_seen", sa.Integer, nullable=True)
    # variant media (pg-native, same shape as Post.data/thumbnail)
    mime_type = sa.Column("mime_type", sa.Text, nullable=False, default="image/webp")
    canvas_width = sa.Column("canvas_width", sa.Integer, nullable=True)
    canvas_height = sa.Column("canvas_height", sa.Integer, nullable=True)
    file_size = sa.Column("file_size", sa.BigInteger, nullable=True)
    variant_sha256 = sa.Column("variant_sha256", sa.LargeBinary, nullable=True)
    # deferred: list selects must never materialise the WebP bodies (same
    # rationale as Post.data/thumbnail — RSS blow-up under fan-out).
    data = deferred(sa.Column("data", sa.LargeBinary, nullable=True))
    thumbnail = deferred(sa.Column("thumbnail", sa.LargeBinary, nullable=True))
    # provenance (estimation, not recovery): engine, seed, mask source
    model = sa.Column("model", sa.Text, nullable=True)
    params = sa.Column("params", postgresql.JSONB, nullable=True)
    # 'unreviewed' | 'approved' | 'rejected' — only 'approved' shown in viewer
    review_state = sa.Column("review_state", sa.Text, nullable=False, default="unreviewed")
    # optimistic-lock / ETag for review-state edits (independent of post.version)
    region_version = sa.Column("region_version", sa.Integer, nullable=False, default=1)
    created_by = sa.Column("created_by", sa.Integer,
                          sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)


# ── Comment ────────────────────────────────────────────────────────────

class CommentScore(Base):
    __tablename__ = "comment_score"

    comment_id = sa.Column("comment_id", sa.Integer, sa.ForeignKey("comment.id"), primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), primary_key=True)
    time = sa.Column("time", sa.DateTime, nullable=False)
    score = sa.Column("score", sa.Integer, nullable=False)


class Comment(Base):
    __tablename__ = "comment"

    comment_id = sa.Column("id", sa.Integer, primary_key=True)
    post_id = sa.Column("post_id", sa.Integer, sa.ForeignKey("post.id"), nullable=False, index=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True)
    version = sa.Column("version", sa.Integer, default=1, nullable=False)
    creation_time = sa.Column("creation_time", sa.DateTime, nullable=False)
    last_edit_time = sa.Column("last_edit_time", sa.DateTime)
    text = sa.Column("text", sa.UnicodeText, default=None)

    user = relationship("User", lazy="joined")
    post = relationship("Post", lazy="select", overlaps="comments")
    scores = relationship("CommentScore", cascade="all, delete-orphan", lazy="joined")


# ── LoRA asset/run registry (migrations 0048 → 0061 split) ───────────────────
# plan:kura-lora-schema-dashboard rev2 P1. lora_model (0048) conflated the
# immutable asset identity with the live progress of ONE training run; 0061
# splits it: lora_asset = what/where/who (asset lifecycle pending→available→
# retired), lora_training_run = 1:N run history (gate-driven state machine,
# see api/lora_ops.py RUN_TRANSITIONS). A compat VIEW named `lora_model`
# (asset ⋈ latest run) keeps legacy consumers reading the old shape during
# the dual-write window; it is retired by a later migration (P2/P3).
class LoraAsset(Base):
    __tablename__ = "lora_asset"

    id = sa.Column("id", sa.Integer, primary_key=True)
    name = sa.Column("name", sa.Text, nullable=False, unique=True)
    # Asset lifecycle only (run state lives on lora_training_run):
    # pending = registered, no usable artifact yet; available; retired.
    status = sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'"))
    origin = sa.Column("origin", sa.Text, nullable=False, server_default=sa.text("'trained'"))
    kind = sa.Column("kind", sa.Text, nullable=True)
    base_model = sa.Column("base_model", sa.Text, nullable=True)
    canonical_path = sa.Column("canonical_path", sa.Text, nullable=True)
    comfy_ref = sa.Column("comfy_ref", sa.Text, nullable=True)
    sha256 = sa.Column("sha256", sa.Text, nullable=True)  # partial-unique WHERE NOT NULL (0061)
    size_bytes = sa.Column("size_bytes", sa.BigInteger, nullable=True)
    created_by = sa.Column("created_by", sa.Text, nullable=True)
    source_url = sa.Column("source_url", sa.Text, nullable=True)
    preview_post_id = sa.Column("preview_post_id", sa.Integer, nullable=True)
    imported_at = sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    notes = sa.Column("notes", sa.Text, nullable=True)

    concepts = relationship("LoraConcept", cascade="all, delete-orphan", backref="lora")
    # Authoritative training source = the exact posts (post-level, migration 0049).
    # lora_pool is a DERIVED VIEW (post→pool rollup), read via SQL, not an ORM table.
    training_posts = relationship("LoraPost", cascade="all, delete-orphan", backref="lora")
    runs = relationship("LoraTrainingRun", cascade="all, delete-orphan", backref="asset")


class LoraTrainingRun(Base):
    """One training run of a LoRA asset (1:N; runs optional per asset).
    state machine: queued→admitted→running→{held,yielded}→{done,failed,cancelled}
    (held/yielded arrive with the gpu_batch_gate integration in P2; valid
    transitions enforced via api/lora_ops.validate_run_transition).
    `attempt` deliberately stays inside job_spec (as-is from 0048) — single
    representation during the dual-write window. backfilled=True marks the
    synthetic historical run rows minted by 0061 for pre-launcher-era assets.

    job_spec.dataset_dir (optional, HOST-absolute kohya dataset path; 0065):
    when the launcher passes it to POST /loras/{id}/start, lora_post/
    lora_concept auto-populate from the dataset — see api/lora_post_resolve.py
    for the resolution ladder. Omit it (still most callers) and nothing about
    training changes; this is how the historically-empty lora_post table below
    gets filled going forward without a separate manual step."""
    __tablename__ = "lora_training_run"

    id = sa.Column("id", sa.Integer, primary_key=True)
    asset_id = sa.Column("asset_id", sa.Integer, sa.ForeignKey("lora_asset.id", ondelete="CASCADE"), nullable=False)
    state = sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'queued'"))
    cur_step = sa.Column("cur_step", sa.Integer, nullable=True)
    total_steps = sa.Column("total_steps", sa.Integer, nullable=True)
    cur_epoch = sa.Column("cur_epoch", sa.Integer, nullable=True)
    eta_seconds = sa.Column("eta_seconds", sa.Integer, nullable=True)
    progress_at = sa.Column("progress_at", sa.DateTime(timezone=True), nullable=True)
    final_loss = sa.Column("final_loss", sa.Float, nullable=True)
    params = sa.Column("params", postgresql.JSONB, nullable=True)
    job_spec = sa.Column("job_spec", postgresql.JSONB, nullable=True)
    exit_code = sa.Column("exit_code", sa.Integer, nullable=True)
    started_at = sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    completed_at = sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    resume_state_path = sa.Column("resume_state_path", sa.Text, nullable=True)
    resume_count = sa.Column("resume_count", sa.Integer, nullable=False, server_default=sa.text("0"))
    backfilled = sa.Column("backfilled", sa.Boolean, nullable=False, server_default=sa.text("false"))
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


class LoraConcept(Base):
    """A trigger word for a LoRA (1:N). `trigger` is matched, case-sensitive,
    against dataset subdir names ({repeats}_{trigger}) by both the manual
    backfill script and the automatic POST /start hook — see
    api/lora_post_resolve.py. A dataset dir whose trigger has no matching row
    here still registers its posts, with concept_id left NULL."""
    __tablename__ = "lora_concept"

    id = sa.Column("id", sa.Integer, primary_key=True)
    lora_id = sa.Column("lora_id", sa.Integer, sa.ForeignKey("lora_asset.id", ondelete="CASCADE"), nullable=False)
    trigger = sa.Column("trigger", sa.Text, nullable=False)
    description = sa.Column("description", sa.Text, nullable=True)


class LoraPost(Base):
    """Authoritative LoRA training source: the exact posts (per concept) a LoRA was
    trained on (migration 0049). The `lora_pool` view rolls this up to pool level.

    Write paths (both funnel through api/lora_post_resolve.py's shared
    resolution ladder, kept in sync on purpose — see its module docstring):
      - automatic: POST /loras/{id}/start's optional job_spec.dataset_dir (0065)
      - manual backfill: scripts/lora_register_posts.py --dataset ... --apply
    A LoRA asset with zero rows here either predates the dataset_dir hookup
    and was never backfilled, or was queued without dataset_dir — not
    necessarily a data problem, but worth checking before assuming one."""
    __tablename__ = "lora_post"

    id = sa.Column("id", sa.Integer, primary_key=True)
    lora_id = sa.Column("lora_id", sa.Integer, sa.ForeignKey("lora_asset.id", ondelete="CASCADE"), nullable=False)
    concept_id = sa.Column("concept_id", sa.Integer, sa.ForeignKey("lora_concept.id", ondelete="SET NULL"), nullable=True)
    post_id = sa.Column("post_id", sa.Integer, nullable=False)


# ── wd14 tagger patch registry + atomic release distribution (migration 0052) ──
# plan:kura-tagger-registry rev2. tagger_patch mirrors tagger/patches/<tag>/v<N>.json
# (git is SoT for the why/why_rejected prose); tagger_release is one immutable row per
# built+freeze-validated merge; tagger_release_active is the singleton CAS pointer,
# only ever updated in the same transaction as the release insert it names (see
# api/routers/tagger.py); tagger_node_state is per-host drift visibility.
class TaggerPatch(Base):
    __tablename__ = "tagger_patch"

    id = sa.Column("id", sa.Integer, primary_key=True)
    tag = sa.Column("tag", sa.Text, nullable=False)
    version = sa.Column("version", sa.Integer, nullable=False)
    status = sa.Column("status", sa.Text, nullable=False)
    patch_sha256 = sa.Column("patch_sha256", sa.Text, nullable=False)
    why = sa.Column("why", sa.Text, nullable=True)
    why_rejected = sa.Column("why_rejected", sa.Text, nullable=True)
    metrics = sa.Column("metrics", postgresql.JSONB, nullable=True)
    base_repo = sa.Column("base_repo", sa.Text, nullable=True)
    base_revision = sa.Column("base_revision", sa.Text, nullable=True)
    logit_index = sa.Column("logit_index", sa.Integer, nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    created_by = sa.Column("created_by", sa.Text, nullable=True)


class TaggerRelease(Base):
    __tablename__ = "tagger_release"

    id = sa.Column("id", sa.Integer, primary_key=True)
    commit_sha = sa.Column("commit_sha", sa.Text, nullable=False)
    base_model_repo = sa.Column("base_model_repo", sa.Text, nullable=False)
    base_model_revision = sa.Column("base_model_revision", sa.Text, nullable=False)
    patch_set_digest = sa.Column("patch_set_digest", sa.Text, nullable=False)
    model_sha256 = sa.Column("model_sha256", sa.Text, nullable=False, unique=True)
    model_data = deferred(sa.Column("model_data", sa.LargeBinary, nullable=False))
    selected_tags_csv = deferred(sa.Column("selected_tags_csv", sa.LargeBinary, nullable=False))
    n_stock = sa.Column("n_stock", sa.Integer, nullable=False)
    n_out = sa.Column("n_out", sa.Integer, nullable=False)
    patched = sa.Column("patched", postgresql.JSONB, nullable=False)
    lockfile = sa.Column("lockfile", sa.Text, nullable=True)
    created_at = sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    created_by = sa.Column("created_by", sa.Text, nullable=True)


class TaggerReleaseActive(Base):
    """Singleton (id always 1) CAS pointer -- see migration 0052 docstring."""
    __tablename__ = "tagger_release_active"

    id = sa.Column("id", sa.Integer, primary_key=True)
    release_id = sa.Column("release_id", sa.Integer, sa.ForeignKey("tagger_release.id"), nullable=True)
    updated_at = sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    updated_by = sa.Column("updated_by", sa.Text, nullable=True)


class TaggerNodeState(Base):
    __tablename__ = "tagger_node_state"

    host = sa.Column("host", sa.Text, primary_key=True)
    release_id = sa.Column("release_id", sa.Integer, sa.ForeignKey("tagger_release.id"), nullable=True)
    verified = sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.text("false"))
    detail = sa.Column("detail", sa.Text, nullable=True)
    reported_at = sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
