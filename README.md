<div align="center">
  <h1>MA4ROM</h1>
  <p><strong>Multi-Agent Framework for Relational-to-Ontology Mapping Generation</strong></p>
  <p><em>Anonymous artifact for double-blind review</em></p>

  <p>
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
    <img alt="PostgreSQL" src="https://img.shields.io/badge/Database-PostgreSQL-4169E1?logo=postgresql&logoColor=white">
    <img alt="OBDA" src="https://img.shields.io/badge/Paradigm-OBDA-0F766E">
    <img alt="Mapping" src="https://img.shields.io/badge/Output-R2RML-7C3AED">
    <img alt="Artifact" src="https://img.shields.io/badge/Status-Review%20Artifact-F59E0B">
  </p>

  <p>
    MA4ROM generates executable relational-to-ontology mappings from relational schemas and target ontologies using a coordinated multi-agent pipeline.
  </p>
</div>

---

# Overview

**MA4ROM** is a multi-agent framework for virtual knowledge graph mapping generation. Given a relational database schema, sampled relational values, and a target ontology, MA4ROM produces executable R2RML mappings and evaluates them with the RODI query-based benchmark protocol.

The framework is designed for difficult mapping cases where simple name matching is insufficient, including adjusted naming, restructured hierarchies, denormalized tables, missing foreign keys, geodata, and large domain-specific schemas such as NPD. The released artifact contains the MA4ROM implementation, packaged RODI-derived datasets, generated mapping outputs, and the evaluation scripts needed to reproduce the reported F1 scores.

<!-- TODO: Add MA4ROM architecture figure here. -->

MA4ROM follows a staged multi-agent workflow:

1. **Mapping Pattern Recognition Agent** identifies schema/ontology mapping patterns such as semantic-equivalent classes, subclass hierarchies, and relationship structures.
2. **Class and Datatype Property Mapping Agent** ranks ontology candidates using lexical similarity, ontology-domain compatibility, and confidence estimation.
3. **Context Enhancement Agent** uses real table values for low-confidence class and datatype-property mappings.
4. **Foreign-Key Completion and Object Property Mapping Agent** handles missing or sparse foreign keys with IND discovery and ontology-aware object-property selection.
5. **R2RML Mapping Generation Agent** writes executable mapping files for downstream virtual knowledge graph evaluation.

---

# Key Contributions

- **Multi-agent mapping generation.** MA4ROM decomposes relational-to-ontology mapping into coordinated agents for pattern recognition, class/datatype-property mapping, object-property mapping, context enhancement, and final R2RML generation.
- **Robust handling of weak relational semantics.** The framework explicitly addresses missing FKs, sparse FK metadata, denormalized schemas, naming shifts, and ontology hierarchy restructuring.
- **Confidence-aware context enhancement.** High-confidence datatype-property mappings can proceed directly, while low-confidence cases are enhanced using real table values to reduce unnecessary LLM calls.
- **Ontology-aware FK and object-property recovery.** MA4ROM combines inclusion-dependency discovery, schema evidence, domain/range constraints, and object-property candidate ranking for FK-missing scenarios.
- **Reproducible RODI artifact.** This repository includes packaged `dump.sql` files, query pairs, generated mapping outputs, and evaluation utilities adapted from the RODI/LLM4VKG evaluation pipeline.

---

# Repository Structure

```text
.
├── ma4rom/                    # MA4ROM source code
│   ├── DPMapping/             # Datatype-property mapping agent
│   ├── OPMapping/             # Object-property mapping modules
│   ├── RealValue/             # Real-value context enhancement
│   ├── experiments/           # Auxiliary experiment scripts
│   ├── utils/                 # DB, ontology, LLM, and ranking utilities
│   ├── config.py              # MA4ROM runtime configuration
│   └── main.py                # Pipeline entry point
├── dataset/                   # Packaged RODI-derived datasets
├── evaluate_results/          # Generated mappings and evaluation outputs
├── resources/                 # Ontop runtime resources
├── src/vkg_utils/             # Evaluation utilities
├── setup_dataset.py           # Dataset checker and PostgreSQL loader
├── run_my_eval.py             # Evaluation entry point
├── rodi_evaluate.py           # Auxiliary evaluation script
├── requirements.txt           # Python dependencies
├── pyproject.toml             # Python project metadata
└── README.md
```

---

# Datasets

The artifact packages RODI-derived scenarios under `dataset/`. Each scenario contains the target ontology, a PostgreSQL dump, and the query pairs used by the evaluator:

```text
dataset/<scenario>/
├── ontology.ttl
├── dump.sql
└── queries/
```

Some scenarios also include the original `ontology.owl` when available. The packaged query pairs are used by `run_my_eval.py` to compute F1 scores from generated mappings.

## Packaged Scenarios

| Scenario family | Included variants | Query pairs per variant |
|---|---|---:|
| CMT | `cmt_renamed`, `cmt_structured`, `cmt_denormalized` and `_-100` variants | 29 |
| Conference | `conference_naive`, `conference_renamed`, `conference_structured`, `conference_nofks` and available `_-100` variants | 39 |
| SIGKDD | `sigkdd_naive`, `sigkdd_renamed`, `sigkdd_structured`, `sigkdd_mixed` and `_-100` variants | 29 |
| Mondial | `mondial_rel`, `mondial_rel_-100` | 50 |
| NPD | `npd_atomic_tests`, `npd_atomic_tests_-100` | 439 |

Validate the packaged datasets with:

```bash
python setup_dataset.py --check
```

List all available scenarios with:

```bash
python setup_dataset.py --list
```

Load one scenario into PostgreSQL:

```bash
python setup_dataset.py --load cmt_structured --drop-existing
```

Load all packaged scenarios:

```bash
python setup_dataset.py --load all --drop-existing
```

The loader uses `PGHOST`, `PGPORT`, `PGUSER`, and `PGPASSWORD` if they are set. Otherwise, it defaults to `localhost:5432` and `postgres/postgres`. It creates one PostgreSQL database per scenario and detects the schema name declared inside each `dump.sql` file.

---

# Installation and Setup

<!-- TODO: Add setup walkthrough screenshot or short demo GIF here. -->

## Prerequisites

| Software | Recommended version | Purpose |
|---|---:|---|
| Python | 3.10+ | MA4ROM pipeline and evaluation scripts |
| PostgreSQL | 14+ | Loading RODI dumps and reading relational schemas |
| Java | 11+ | Ontop-based evaluation runtime |
| Git | Any recent version | Cloning the artifact repository |

The evaluation scripts use the bundled Ontop runtime under `resources/ontop/`.

## Step 1: Clone the Repository

```bash
git clone https://github.com/anonymous-ma4rom/ma4rom-anonymous.git
cd ma4rom-anonymous
```

## Step 2: Create a Python Environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Step 3: Validate and Load the Dataset

First check that all packaged files are present:

```bash
python setup_dataset.py --check
```

Then load the target scenario into PostgreSQL:

```bash
python setup_dataset.py --load cmt_structured --drop-existing
```

For non-default PostgreSQL credentials, either export environment variables:

```bash
export PGHOST=localhost
export PGPORT=5432
export PGUSER=postgres
export PGPASSWORD=postgres
python setup_dataset.py --load cmt_structured --drop-existing
```

or pass command-line options:

```bash
python setup_dataset.py --load cmt_structured --host localhost --port 5432 --user postgres --password postgres --drop-existing
```

After loading, the script prints the exact `MAMG_CURRENT_DATABASE` and, when needed, `MAMG_DB_SCHEMA` command for running MA4ROM.

## Step 4: Configure MA4ROM

Open `ma4rom/config.py` and set the LLM configuration if you want to rerun the mapping-generation pipeline:

```python
LLM_API_KEY = "YOUR_API_KEY"
LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"
```

The PostgreSQL connection defaults to:

```python
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": CURRENT_DATABASE,
    "user": "postgres",
    "password": "postgres",
}
```

If your local PostgreSQL username or password differs, edit `DB_CONFIG` accordingly.

## Step 5: Run MA4ROM

Run a single scenario from the repository root:

```bash
MAMG_CURRENT_DATABASE=cmt_structured python ma4rom/main.py
```

For scenarios whose dump schema differs from the scenario name, use the command printed by `setup_dataset.py`. For example:

```bash
MAMG_CURRENT_DATABASE=mondial_rel MAMG_DB_SCHEMA=mondial_rdf2sql_standard python ma4rom/main.py
```

Generated files are written under `ma4rom/output/<scenario>/`, including:

```text
rodi_<scenario>_generated_mapping.ttl
rodi_<scenario>_generated_ontology.ttl
ontology.properties
metrics/intermediate JSON files
```

---

# Evaluation

This repository includes generated outputs and evaluation scripts so reviewers can inspect existing results or recompute F1 scores.

## Evaluate One Result Directory

```bash
python run_my_eval.py evaluate_results/default/cmt_structured_test
```

## Evaluate All Results in a Folder

```bash
python run_my_eval.py evaluate_results/default --batch --skip-done
```

Each evaluated result directory should contain:

```text
rodi_<scenario>_generated_mapping.ttl
rodi_<scenario>_generated_ontology.ttl
ontology.properties
```

The evaluator reads query pairs from:

```text
dataset/<scenario>/queries/
```

and writes the following files into the evaluated result directory:

```text
metrics_details.json
f1.txt
```

## Included Result Groups

| Directory | Description |
|---|---|
| `evaluate_results/default/` | Main MA4ROM generated mappings and evaluation outputs |
| `evaluate_results/ablations/` | Ablation outputs for MA4ROM components |
| `evaluate_results/delete_all_fks/` | Robustness results after removing foreign keys |
| `evaluate_results/hyper_dp_weight_*` | Datatype-property weight sensitivity outputs |
| `evaluate_results/hyper_dp_confidence_*` | Datatype-property confidence-threshold sensitivity outputs |

---

# Main Experimental Settings

The main paper settings are encoded in `ma4rom/config.py`. The most important hyperparameters are:

| Parameter | Value | Meaning |
|---|---:|---|
| `DP_MAPPING_CANDIDATE_TEXT_WEIGHT` | `0.7` | Lexical score weight for datatype-property ranking |
| `DP_MAPPING_CANDIDATE_DOMAIN_WEIGHT` | `0.3` | Domain-compatibility score weight |
| `DP_MAPPING_CONF_HIGH_TOP1` | `0.7` | Top-1 score threshold for high confidence |
| `DP_MAPPING_CONF_HIGH_GAP` | `0.2` | Top-1/top-2 score gap for high confidence |
| `FK_COMPLETION_IND_THRESHOLD` | `0.95` | Inclusion-dependency threshold for FK completion |
| LLM temperature | `0` | Deterministic LLM generation setting |

---

# Reproducibility Checklist

- [x] Source code for MA4ROM pipeline
- [x] Packaged RODI-derived `dump.sql` files
- [x] Packaged query pairs under `dataset/<scenario>/queries/`
- [x] Dataset validation and PostgreSQL setup script
- [x] Evaluation scripts and Ontop resources
- [x] Generated mapping outputs and F1 result files
- [x] Anonymous metadata for double-blind review

---

# Notes for Double-Blind Review

This repository is prepared for anonymous review. Author names, affiliations, personal paths, and identifying metadata have been removed. Full author information and citation metadata will be added after the review process.

---

# Citation

Citation information will be added after publication.
