# Model Registry Client

Model Registry Client — cross-cutting, no single owning module. Shared client for reading and writing model configuration and version records. Write logic belongs to whichever module first needs to write a given version type (Module 08 for `feature_schema_version`, Modules 10/13 for `target_model_version`/`scoring_configuration`).
