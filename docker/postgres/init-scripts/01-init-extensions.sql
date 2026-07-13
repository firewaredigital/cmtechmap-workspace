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
SELECT 'CREATE DATABASE keycloak OWNER cm_techmap'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec
