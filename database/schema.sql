-- Kura 1.0 — Canonical Schema (production DDL)
--
-- Status:  Draft / v0.1.0
-- Realizes the Kura 1.0 design (KTS-HANDOFF-001 + PRs #21-#28) now that the §9
-- reconciliation gate is cleared. Baseline snapshot; forward changes go through
-- database/migrations/. Binding invariants (Volume 2, ADR-0001/0002):
--   * PostgreSQL authoritative; canonical state is RELATIONAL — NO json/jsonb.
--   * Observations are APPEND-ONLY (immutability trigger; UPDATE/DELETE rejected).
--   * Geometry / regions / poses / attributes attach to ENTITIES.
--   * Ontology is DATA (loaded from database/seed/*), not hardcoded.
--   * Every ontology entry has a stable slug; scenes/assets/entities/observations
--     carry a stable uuid.
--
-- Requires PostgreSQL 13+ (gen_random_uuid built-in). Tested on PG 18.

CREATE SCHEMA IF NOT EXISTS kura;
SET search_path = kura, public;

-- =====================================================================
-- 1. Immutability guard (append-only enforcement for observations)
-- =====================================================================
CREATE OR REPLACE FUNCTION kura.trg_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only: % on % is not permitted (corrections are new observations, ADR-0001)',
        TG_OP, TG_TABLE_NAME;
END;
$$;

-- =====================================================================
-- 2. Controlled vocabularies (lookup tables — no free-text domains)
-- =====================================================================
CREATE TABLE entity_kind          (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE gender_presentation  (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE attachment_scope     (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE observation_domain   (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE event_type           (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE evidence_type        (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE producer_kind        (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE asset_role           (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE resolution_status    (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE attribute_category   (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE attribute_value_type (slug text PRIMARY KEY, description text NOT NULL);

INSERT INTO entity_kind(slug,description) VALUES
  ('person','A person entity'),('object','A non-person object entity');
INSERT INTO gender_presentation(slug,description) VALUES
  ('female','Female presentation'),('male','Male presentation'),('other','Other/non-binary'),
  ('androgynous','Androgynous'),('ambiguous','Ambiguous/undetermined');
INSERT INTO attachment_scope(slug,description) VALUES
  ('entity','Attached to an unambiguous entity'),('scene','Scene-level'),
  ('unresolved','Person-scoped but entity ambiguous — pending disambiguation');
INSERT INTO observation_domain(slug,description) VALUES
  ('body_region','Body-region presence'),('attribute','Attribute value'),
  ('pose','Pose descriptor'),('relation','Inter/intra-entity relation'),
  ('scene_descriptor','Scene facet descriptor'),('identity','Species/character/franchise'),
  ('entity_count','Entity count/instantiation'),('object','Object presence/state');
INSERT INTO event_type(slug,description) VALUES
  ('model_annotation','Produced by a model'),('human_annotation','Produced by a human'),
  ('resolution','Resolver-derived'),('retraction','Retracts a prior observation');
INSERT INTO evidence_type(slug,description) VALUES
  ('explicit_add','Explicitly added'),('explicit_remove','Explicitly removed'),
  ('focused_retained','Retained under focused review'),('focused_uncertain','Uncertain under review');
INSERT INTO producer_kind(slug,description) VALUES ('model','A model'),('human','A human');
INSERT INTO asset_role(slug,description) VALUES
  ('source','Source depiction of a scene'),('generation_target','A generation target'),
  ('generated_output','An observed generated output');
INSERT INTO resolution_status(slug,description) VALUES
  ('current','Current resolved version'),('superseded','Superseded by a newer resolution');
INSERT INTO attribute_category(slug,description) VALUES
  ('appearance','Stable visual property'),('state','Transient condition'),('measure','Continuous quantity');
INSERT INTO attribute_value_type(slug,description) VALUES
  ('categorical','Enumerated values'),('continuous','Scalar with interval'),('boolean','Present/absent');

-- =====================================================================
-- 3. Storage (content-addressed blobs, external — no inline data)
-- =====================================================================
CREATE TABLE blob_backend (
    slug text PRIMARY KEY,
    description text NOT NULL);
CREATE TABLE blob (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256       bytea NOT NULL UNIQUE,
    backend_slug text NOT NULL REFERENCES blob_backend(slug),
    external_ref text NOT NULL,
    byte_size    bigint,
    media_type   text);

-- =====================================================================
-- 4. Ontology — body
-- =====================================================================
CREATE TABLE region_class (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE body_region (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug          text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    parent_slug   text REFERENCES body_region(slug),
    laterality    text NOT NULL CHECK (laterality IN ('none','midline','bilateral')),
    primary_class text NOT NULL REFERENCES region_class(slug));
CREATE TABLE region_class_map (
    region_slug text NOT NULL REFERENCES body_region(slug),
    class_slug  text NOT NULL REFERENCES region_class(slug),
    PRIMARY KEY (region_slug, class_slug));
CREATE TABLE keypoint (
    scheme        text NOT NULL,
    keypoint_slug text NOT NULL,
    PRIMARY KEY (scheme, keypoint_slug));
CREATE TABLE region_keypoint (
    region_slug   text NOT NULL REFERENCES body_region(slug),
    laterality    text NOT NULL CHECK (laterality IN ('na','left','right')),
    scheme        text NOT NULL DEFAULT 'kura-skeleton-v0',
    keypoint_slug text NOT NULL,
    role          text NOT NULL CHECK (role IN ('is','anchors')),
    FOREIGN KEY (scheme, keypoint_slug) REFERENCES keypoint(scheme, keypoint_slug));

-- =====================================================================
-- 5. Ontology — attribute
-- =====================================================================
CREATE TABLE attribute (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug          text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    category      text NOT NULL REFERENCES attribute_category(slug),
    value_type    text NOT NULL REFERENCES attribute_value_type(slug),
    applies_kind  text NOT NULL CHECK (applies_kind IN ('class','region','entity')),
    applies_key   text NOT NULL,
    range_min     numeric,
    range_max     numeric,
    unit          text);
CREATE TABLE attribute_value (
    attribute_slug text NOT NULL REFERENCES attribute(slug),
    value_slug     text NOT NULL,
    PRIMARY KEY (attribute_slug, value_slug));

-- =====================================================================
-- 6. Ontology — relation
-- =====================================================================
CREATE TABLE relation (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug           text NOT NULL UNIQUE,
    display_name   text NOT NULL,
    category       text NOT NULL CHECK (category IN ('spatial','contact','interaction')),
    arity          text NOT NULL DEFAULT 'binary',
    directionality text NOT NULL CHECK (directionality IN ('directed','symmetric')));
CREATE TABLE relation_applicability (
    relation_slug    text NOT NULL REFERENCES relation(slug),
    role             text NOT NULL,
    participant_kind text NOT NULL CHECK (participant_kind IN ('entity','class','region')),
    participant_key  text NOT NULL);

-- =====================================================================
-- 7. Ontology — scene
-- =====================================================================
CREATE TABLE scene_facet (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE scene_descriptor (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug         text NOT NULL UNIQUE,
    display_name text NOT NULL,
    facet        text NOT NULL REFERENCES scene_facet(slug));

-- =====================================================================
-- 8. Ontology — object
-- =====================================================================
CREATE TABLE object_category (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE clothing_slot   (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE object_state    (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE object_type (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug          text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    category      text NOT NULL REFERENCES object_category(slug),
    slot          text REFERENCES clothing_slot(slug),
    participation text NOT NULL);
CREATE TABLE object_coverage (
    object_slug text NOT NULL REFERENCES object_type(slug),
    covered_key text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('region','class')));

-- =====================================================================
-- 9. Ontology — pose
-- =====================================================================
CREATE TABLE pose_scope    (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE pose_category (slug text PRIMARY KEY, description text NOT NULL);
CREATE TABLE pose (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug         text NOT NULL UNIQUE,
    display_name text NOT NULL,
    scope        text NOT NULL REFERENCES pose_scope(slug),
    category     text NOT NULL REFERENCES pose_category(slug));
CREATE TABLE pose_keypoint (
    pose_slug     text NOT NULL REFERENCES pose(slug),
    scheme        text NOT NULL DEFAULT 'kura-skeleton-v0',
    keypoint_slug text NOT NULL,
    FOREIGN KEY (scheme, keypoint_slug) REFERENCES keypoint(scheme, keypoint_slug));
CREATE TABLE pose_region (
    pose_slug   text NOT NULL REFERENCES pose(slug),
    region_slug text NOT NULL REFERENCES body_region(slug));

-- =====================================================================
-- 10. Ontology — identity (species / franchise / character)
-- =====================================================================
CREATE TABLE species (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE, display_name text NOT NULL, marked_by text);
CREATE TABLE franchise (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE, display_name text NOT NULL);
CREATE TABLE character (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE, display_name text NOT NULL,
    franchise_slug  text REFERENCES franchise(slug),
    primary_species text REFERENCES species(slug));
CREATE TABLE identity_modifier (slug text PRIMARY KEY, description text NOT NULL);

-- =====================================================================
-- 11. Scenes & assets
-- =====================================================================
CREATE TABLE scene (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid       uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE asset (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid       uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    scene_id   bigint NOT NULL REFERENCES scene(id),
    role       text NOT NULL REFERENCES asset_role(slug),
    blob_id    bigint REFERENCES blob(id),
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE asset_relation (
    from_asset_id bigint NOT NULL REFERENCES asset(id),
    to_asset_id   bigint NOT NULL REFERENCES asset(id),
    relation_slug text NOT NULL,
    PRIMARY KEY (from_asset_id, to_asset_id, relation_slug));
CREATE TABLE asset_region (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id        bigint NOT NULL REFERENCES asset(id),
    source_asset_id bigint REFERENCES asset(id),
    x_min real, y_min real, x_max real, y_max real);

-- =====================================================================
-- 12. Entities (scene-scoped; attachments hang off these)
-- =====================================================================
CREATE TABLE entity (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    scene_id            bigint NOT NULL REFERENCES scene(id),
    kind                text NOT NULL REFERENCES entity_kind(slug),
    gender_presentation text REFERENCES gender_presentation(slug),  -- persons only
    object_slug         text REFERENCES object_type(slug),          -- objects only
    CHECK ( (kind='person' AND object_slug IS NULL)
         OR (kind='object' AND gender_presentation IS NULL) ));
CREATE TABLE entity_region (            -- an entity's instance of a body region
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id   bigint NOT NULL REFERENCES entity(id),
    region_slug text NOT NULL REFERENCES body_region(slug),
    laterality  text NOT NULL DEFAULT 'na' CHECK (laterality IN ('na','left','right')),
    UNIQUE (entity_id, region_slug, laterality));

-- =====================================================================
-- 13. Observations (append-only base + per-domain typed details)
--     Relational, JSON-free. Immutability enforced by trigger.
-- =====================================================================
CREATE TABLE observation (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid              uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    scene_id          bigint NOT NULL REFERENCES scene(id),
    subject_scope     text NOT NULL REFERENCES attachment_scope(slug),
    subject_entity_id bigint REFERENCES entity(id),   -- NULL for scene/unresolved
    domain            text NOT NULL REFERENCES observation_domain(slug),
    event_type        text NOT NULL REFERENCES event_type(slug),
    producer_kind     text NOT NULL REFERENCES producer_kind(slug),
    producer_ref      text NOT NULL,        -- model id+version or human account
    producer_version  text,
    evidence_type     text REFERENCES evidence_type(slug),
    retracts_id       bigint REFERENCES observation(id),
    observed_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ( (subject_scope='entity') = (subject_entity_id IS NOT NULL) ));

CREATE TABLE obs_body_region (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    region_slug    text NOT NULL REFERENCES body_region(slug),
    present        boolean NOT NULL DEFAULT true);
CREATE TABLE obs_attribute (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    attribute_slug text NOT NULL REFERENCES attribute(slug),
    value_slug     text,                 -- categorical/boolean
    value_numeric  numeric,              -- continuous point estimate
    interval_low   numeric,              -- continuous uncertainty
    interval_high  numeric,
    FOREIGN KEY (attribute_slug, value_slug) REFERENCES attribute_value(attribute_slug, value_slug));
CREATE TABLE obs_pose (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    pose_slug      text NOT NULL REFERENCES pose(slug));
CREATE TABLE obs_relation (
    observation_id    bigint PRIMARY KEY REFERENCES observation(id),
    relation_slug     text NOT NULL REFERENCES relation(slug),
    subject_entity_id bigint REFERENCES entity(id),
    object_entity_id  bigint REFERENCES entity(id));
CREATE TABLE obs_scene_descriptor (
    observation_id  bigint PRIMARY KEY REFERENCES observation(id),
    descriptor_slug text NOT NULL REFERENCES scene_descriptor(slug));
CREATE TABLE obs_identity (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    species_slug   text REFERENCES species(slug),
    character_slug text REFERENCES character(slug),
    modifier_slug  text REFERENCES identity_modifier(slug));
CREATE TABLE obs_object (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    object_slug    text NOT NULL REFERENCES object_type(slug),
    state_slug     text REFERENCES object_state(slug));
CREATE TABLE obs_entity_count (
    observation_id bigint PRIMARY KEY REFERENCES observation(id),
    presentation   text NOT NULL REFERENCES gender_presentation(slug),
    count_min      integer NOT NULL,
    count_exact    boolean NOT NULL DEFAULT true);

-- attach append-only guard to every observation table
CREATE TRIGGER ao_observation      BEFORE UPDATE OR DELETE ON observation          FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_body_region  BEFORE UPDATE OR DELETE ON obs_body_region      FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_attribute    BEFORE UPDATE OR DELETE ON obs_attribute        FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_pose         BEFORE UPDATE OR DELETE ON obs_pose             FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_relation     BEFORE UPDATE OR DELETE ON obs_relation         FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_scene_desc   BEFORE UPDATE OR DELETE ON obs_scene_descriptor FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_identity     BEFORE UPDATE OR DELETE ON obs_identity         FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_object       BEFORE UPDATE OR DELETE ON obs_object           FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();
CREATE TRIGGER ao_obs_entity_count BEFORE UPDATE OR DELETE ON obs_entity_count     FOR EACH ROW EXECUTE FUNCTION kura.trg_append_only();

CREATE INDEX ix_observation_scene  ON observation(scene_id);
CREATE INDEX ix_observation_entity ON observation(subject_entity_id);
CREATE INDEX ix_observation_domain ON observation(domain);

-- =====================================================================
-- 14. Resolution & projections (Resolver-owned; versioned)
-- =====================================================================
CREATE TABLE resolver_run (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resolver_version text NOT NULL,
    run_at           timestamptz NOT NULL DEFAULT now());
CREATE TABLE resolved_scene (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scene_id        bigint NOT NULL REFERENCES scene(id),
    resolver_run_id bigint NOT NULL REFERENCES resolver_run(id),
    status          text NOT NULL REFERENCES resolution_status(slug),
    resolved_at     timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX ux_resolved_scene_current
    ON resolved_scene(scene_id) WHERE status='current';
-- a resolved-scene references the observations it accepted (provenance of truth)
CREATE TABLE resolved_observation (
    resolved_scene_id bigint NOT NULL REFERENCES resolved_scene(id),
    observation_id    bigint NOT NULL REFERENCES observation(id),
    PRIMARY KEY (resolved_scene_id, observation_id));
-- derived tag projection (tags are NOT canonical — rebuildable from resolved_scene)
CREATE TABLE tag_projection (
    resolved_scene_id bigint NOT NULL REFERENCES resolved_scene(id),
    tag               text NOT NULL,
    PRIMARY KEY (resolved_scene_id, tag));

-- =====================================================================
-- 15. Provenance / collections / models / review  (carry-over from legacy)
-- =====================================================================
CREATE TABLE source (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE, kind text NOT NULL);
CREATE TABLE source_account (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_slug text NOT NULL REFERENCES source(slug),
    platform text, account_ref text NOT NULL,
    UNIQUE (source_slug, account_ref));
CREATE TABLE asset_source (
    asset_id          bigint NOT NULL REFERENCES asset(id),
    source_slug       text NOT NULL REFERENCES source(slug),
    source_account_id bigint REFERENCES source_account(id),
    external_id       text,
    PRIMARY KEY (asset_id, source_slug));
CREATE TABLE collection (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    title text);
CREATE TABLE collection_asset (
    collection_id bigint NOT NULL REFERENCES collection(id),
    asset_id      bigint NOT NULL REFERENCES asset(id),
    position      integer NOT NULL,
    PRIMARY KEY (collection_id, asset_id));
CREATE TABLE model_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE, version text NOT NULL, weight_blob_id bigint REFERENCES blob(id));
CREATE TABLE review_pool (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    focus_scope text, opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz);
CREATE TABLE review_item (
    review_pool_id bigint NOT NULL REFERENCES review_pool(id),
    asset_id       bigint NOT NULL REFERENCES asset(id),
    PRIMARY KEY (review_pool_id, asset_id));

COMMENT ON SCHEMA kura IS 'Kura 1.0 canonical schema v0.1.0 — scene/entity-centric, append-only observations, Resolver-derived truth, relational (no JSON canonical).';
COMMENT ON TABLE observation IS 'Append-only evidence (ADR-0001). Corrections are new rows; retractions set retracts_id. UPDATE/DELETE rejected by trigger.';
COMMENT ON TABLE tag_projection IS 'Derived projection of a resolved scene (INV-7) — rebuildable, never canonical truth.';
