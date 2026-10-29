#!/bin/sh
# Run once by the postgres image, on an empty data directory (docker-entrypoint-initdb.d), as
# its superuser: what ops/install/database.py create does on a host install — the product's
# roles, the database owned by kura, the extensions, then the schema baseline loaded as the
# superuser so the owners it names hold. The password arrives in the environment and goes to
# psql on stdin, never in an argument.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
SET password_encryption = 'scram-sha-256';
CREATE ROLE kura LOGIN PASSWORD '$KURA_DB_PASSWORD';
CREATE ROLE kura_reconciler NOLOGIN;
CREATE ROLE kura_audit NOLOGIN;
CREATE DATABASE kura OWNER kura TEMPLATE template0 ENCODING 'UTF8';
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname kura -q <<SQL
CREATE EXTENSION vector; CREATE EXTENSION pg_trgm; CREATE EXTENSION fuzzystrmatch;
SQL
baseline=$(ls /kura-baseline/*.sql | sort | tail -n 1)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname kura -q -f "$baseline"
echo "loaded $baseline into kura"
