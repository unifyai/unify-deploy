# Client Ingestion Scripts

Generic offline scripts for ingesting client data via DataManager or
FileManager pipelines.  These scripts are client-agnostic — the
`--client` and `--deployment` arguments determine which pipeline config
and data paths are used.

## DataManager CLI (`ingest_dm.py`)

Parses Excel files and ingests rows via `DataManager.ingest()`.

```bash
# Client Alpha, v1 deployment
uv run unity_deploy/assistant_deployments/scripts/ingest_dm.py \
  --client client_alpha --project ClientAlpha --deployment v1

# With options
uv run unity_deploy/assistant_deployments/scripts/ingest_dm.py \
  --client client_alpha --project ClientAlpha --deployment v1 \
  --parallel --no-embed --debug --no-sdk-log

# Only specific tables
uv run unity_deploy/assistant_deployments/scripts/ingest_dm.py \
  --client client_alpha --project ClientAlpha \
  --tables "July 2025" "August 2025"
```

### Merge per-table configs

```bash
uv run unity_deploy/assistant_deployments/scripts/ingest_dm.py \
  --client client_alpha --deployment v1 --project ClientAlpha \
  --merge-configs \
    unity_deploy/assistant_deployments/clients/client_alpha/deployments/v1/data/configs
```

## FileManager CLI (`ingest_fm.py`)

Translates `pipeline_config.json` into a `FilePipelineConfig` and calls
`FileManager.ingest_files()`.

```bash
uv run unity_deploy/assistant_deployments/scripts/ingest_fm.py \
  --client client_alpha --project ClientAlpha --deployment v1

uv run unity_deploy/assistant_deployments/scripts/ingest_fm.py \
  --client client_alpha --project ClientAlpha \
  --parallel --progress-file ./logs/fm_progress.jsonl
```

## CLI arguments

### Shared (both scripts)

| Argument | Default | Description |
|----------|---------|-------------|
| `--client` | required | Client package name under `clients/` |
| `--deployment` | `v1` | Folder under `deployments/` |
| `--config` | auto | Path to `pipeline_config.json` |
| `--project` | required | Unify project name |
| `--overwrite` | off | Delete and recreate project first |
| `--no-embed` | off | Skip embedding |
| `--verbosity` | from config | `low` / `medium` / `high` |
| `--progress-file` | auto | Override progress JSONL output path |
| `--log-file` | auto | Override log file path |
| `--debug` | off | Enable DEBUG-level Python logging |
| `--no-sdk-log` | off | Suppress the `*_unify.log` SDK log file |
| `--merge-configs DIR` | — | Merge `DIR/*.json` into config then exit |

### `ingest_dm.py` only

| Argument | Default | Description |
|----------|---------|-------------|
| `--parallel` | off | Ingest tables in parallel |
| `--skip-all-context` | off | Skip `add_to_all_context` |
| `--chunk-size` | per-table | Override rows-per-chunk |
| `--tables` | all | Only ingest listed tables (substring match) |

### `ingest_fm.py` only

| Argument | Default | Description |
|----------|---------|-------------|
| `--parallel` | off | Process files in parallel |

## Progress output

Both scripts write to a timestamped run directory under `logs/pipeline/`:

```
logs/pipeline/
  ingest_dm/
    2026-04-06T17-38-41/
      run.log
      run_unify.log
      progress.jsonl
      error_details/
  ingest_fm/
    ...
```
