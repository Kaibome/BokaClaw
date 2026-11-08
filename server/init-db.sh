#!/bin/bash
set -e

# Create the application database (users, conversations, runs, and the derived memory index).
# The default 'postgres' database holds legacy Mem0 tables used only by the one-time migration.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE mem0_app'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mem0_app')\gexec
EOSQL
