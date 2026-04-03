-- Fix: Add unique constraint to system_parameters.name
-- This allows ON CONFLICT (name) upserts to work correctly

ALTER TABLE system_parameters
  ADD CONSTRAINT system_parameters_name_unique UNIQUE (name);

-- After this migration, connector download URLs can be upserted using:
-- INSERT INTO system_parameters (name, value, description, category, value_type)
-- VALUES ...
-- ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value;
