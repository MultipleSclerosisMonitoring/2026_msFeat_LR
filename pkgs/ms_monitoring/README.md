[← Project Home](../README.md)

# ms_monitoring

Command-line tools for extracting and processing wearable activity data
in multiple sclerosis monitoring studies.

## High-Level Workflow

### 1. find_mscodeids

![Sequence Diagram illustrating the find_mscodeids CLI flow](../static/find_mscodeids_flow.png)

### 2. find_gait

![Sequence Diagram illustrating the find_gait CLI flow](../static/find_gait_flow.png)

## Requirements

- Python 3.12 or higher  
- A `config.yaml` in the project root defining your InfluxDB and PostgreSQL connections:

  ```yaml
  influxdb:
    url:      "https://<host>:8086"
    token:    "<YOUR_TOKEN>"
    org:      "<ORG>"
    bucket:   "<BUCKET>"
    measurement: "<MEASUREMENT>"
    verify:   false
    timeout:  900000

  postgresql:
    host:     "<PG_HOST>"
    port:     5432
    user:     "<USER>"
    password: "<PASSWORD>"
    database: "<DB_NAME>"
  ```

## Installation

```bash
# From the repository root:
pip install -r requirements.txt
```

## Usage

### 1. find_mscodeids

Extracts unique device CodeIDs, identifies activity segments (“Left” & “Right” foot),
and writes to PostgreSQL tables `activity_leg` and `activity_all`.

```bash
python -m ms_monitoring.find_mscodeids   -c config.yaml   [-f "YYYY-MM-DD HH:MM:SS"]   [-u "YYYY-MM-DD HH:MM:SS"]   [-l en]   [-v N]   [--head-rows M]
```

- `-c, --config`   Path to YAML config (required).
- `-f, --from`     Start datetime (default: yesterday at 00:00:00).
- `-u, --until`    End datetime (default: now).
- `-l, --lang`     Interface language (`en`, `es`; default: `en`).
- `-v, --verbose`  Verbosity level (0–3+).
- `--head-rows`    Rows to preview when `-v ≥ 2` (default: 5).

### 2. find_gait

Processes stored activity segments, applies power/time-based checks
to detect effective movements and gait, and optionally saves results.

```bash
python -m ms_monitoring.find_gait   -c config.yaml   -i "[ID1,ID2,...]"   [-l en]   [--output raw_data.xlsx]   [--head-rows N]   [--save]   [-v N]
```

- `-c, --config`   Path to YAML config (required).
- `-i, --ids`      JSON list of `activity_all` record IDs (required).
- `-l, --lang`     Interface language (`en`, `es`; default: `en`).
- `--output`       Optional XLSX export of raw sensor data.
- `--head-rows`    Rows to preview when `-v ≥ 2` (default: 5).
- `--save`         Persist results in tables `effective_movement` & `effective_gait`.
- `-v, --verbose`  Verbosity level (0–2).

## SQL Schema Extension

In addition to the standard tables (`codeids`, `activity_leg`, `activity_all`,
`effective_movement`), the following table is created to store gait episodes:

```sql
CREATE TABLE IF NOT EXISTS effective_gait (
  id          SERIAL PRIMARY KEY,
  codeid_id   INT REFERENCES codeids(id),
  start_time  TIMESTAMPTZ NOT NULL,
  end_time    TIMESTAMPTZ NOT NULL,
  duration    NUMERIC NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effective_gait_codeid 
  ON effective_gait(codeid_id);
```

## License

This project is released under the **MIT License**. See the `LICENSE` file for details.
