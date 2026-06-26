# MA4ROM: Multi-Agent Framework for Relational-to-Ontology Mapping Generation

This repository contains the anonymized implementation and evaluation artifact for **MA4ROM**, a multi-agent framework for relational-to-ontology mapping generation.

MA4ROM takes a relational database schema and a target ontology as input, and generates executable R2RML mappings through four collaborative agents:

1. Mapping Pattern Recognition Agent
2. Class and Datatype Property Mapping Agent
3. Object Property Mapping Agent
4. R2RML Mapping Generation Agent

The framework targets semantic loss in schema names, enumerated-value subclass mapping, missing foreign keys, and object property mapping under weak relational semantics.

## Repository Structure

```text
.
├── ma4rom/                 # MA4ROM mapping-generation implementation
├── dataset/                # RODI benchmark data used by the artifact
├── evaluate_results/       # Generated mappings and evaluation outputs
├── setup_dataset.py        # Dataset validation and PostgreSQL loading helper
├── run_my_eval.py          # Evaluation entry point
├── rodi_evaluate.py        # Auxiliary evaluation script
├── src/vkg_utils/          # RODI/Ontop evaluation utilities
├── resources/              # Ontop runtime resources
├── config.py               # Local database configuration for evaluation
└── README.md
```

## Dataset

The benchmark data are derived from the RODI dataset. Each scenario folder under `dataset/` contains the ontology, SQL dump, and query pairs used for evaluation:

```text
dataset/<scenario>/
├── ontology.ttl
├── dump.sql
└── queries/
```

The original RODI data can be obtained from the RODI benchmark distribution. In this artifact, `dump.sql` and `queries/` are included for the evaluated scenarios so that reviewers can reproduce the database loading and query-based evaluation.

Validate the packaged datasets with:

```bash
python setup_dataset.py --check
```

List available scenarios with:

```bash
python setup_dataset.py --list
```

Load all scenarios into local PostgreSQL databases with:

```bash
python setup_dataset.py --load all --drop-existing
```

Load one scenario only with:

```bash
python setup_dataset.py --load cmt_structured --drop-existing
```

By default, the script uses `PGHOST`, `PGPORT`, `PGUSER`, and `PGPASSWORD` when available, otherwise `localhost:5432` and `postgres/postgres`. Each scenario is loaded into a PostgreSQL database with the same name as the scenario. The script detects the schema created by each dump and prints the matching `MAMG_CURRENT_DATABASE` / `MAMG_DB_SCHEMA` command after loading.

## Requirements

The code was tested with Python 3.10+ and PostgreSQL. Install Python dependencies with:

```bash
pip install -r requirements.txt
```

The evaluation uses Ontop through the bundled `resources/ontop` runtime. PostgreSQL should be running locally, and the database connection can be configured in `config.py`:

```python
db_config = {
    "user": "postgres",
    "password": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "rodi"
}
```

## Running MA4ROM

Run the mapping-generation pipeline from the repository root after loading the corresponding PostgreSQL database:

```bash
MAMG_CURRENT_DATABASE=cmt_structured python ma4rom/main.py
```

MA4ROM automatically uses the schema name expected by the packaged RODI dumps. If a custom PostgreSQL schema is used, override it with:

```bash
MAMG_CURRENT_DATABASE=cmt_structured MAMG_DB_SCHEMA=custom_schema python ma4rom/main.py
```

Generated mappings are written to the corresponding output directory configured by the pipeline.

## Evaluation

The evaluation scripts are adapted from the RODI/LLM4VKG evaluation pipeline and copied into this artifact for reproducibility.

Evaluate a single generated result directory:

```bash
python run_my_eval.py evaluate_results/default/cmt_structured
```

Evaluate all immediate subdirectories under a parent result directory:

```bash
python run_my_eval.py evaluate_results/default --batch --skip-done
```

Each evaluated result directory is expected to contain:

```text
rodi_<scenario>_generated_mapping.ttl
rodi_<scenario>_generated_ontology.ttl
ontology.properties
```

The evaluator reads query pairs from:

```text
dataset/<scenario>/queries/
```

and writes:

```text
metrics_details.json
f1.txt
```

to the evaluated result directory.

## Main Experimental Settings

The main hyperparameters used in the paper are:

```text
lambda_lex = 0.7
lambda_dom = 0.3
theta_h = 0.7
Delta_h = 0.2
theta_IND = 0.95
```

LLM-based components are run with temperature 0.

## Anonymity Notice

This repository is prepared for double-blind review. Author names, affiliations, personal paths, and identifying metadata have been removed. Full author information will be added after the review process.

## Citation

Citation information will be added after publication.
