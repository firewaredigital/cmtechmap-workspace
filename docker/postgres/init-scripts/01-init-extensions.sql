-- ==============================================================================
-- CM TECHMAP — PostgreSQL Initialization
-- Creates PostGIS extension, secondary databases, and base schemas
-- ==============================================================================

-- Enable PostGIS spatial extension
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- Trigram search for fuzzy matching
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- UUID generation

-- Verify PostGIS installation
SELECT PostGIS_Full_Version();

-- Create Keycloak database if it doesn't exist
-- (PostgreSQL init scripts run against the main DB, so we create KC DB separately)
-- Owner is resolved dynamically: POSTGRES_USER differs per environment
-- (cm_techmap in dev, cm_techmap_prod in prod, cm_techmap_oci on OCI) —
-- hardcoding it broke Keycloak DB creation everywhere except dev.
SELECT format('CREATE DATABASE keycloak OWNER %I', current_user)
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
