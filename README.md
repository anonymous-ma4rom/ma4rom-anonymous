<div align="center">
  <h1>MA4ROM</h1>
  <p><strong>Multi-Agent Framework for Relational-to-Ontology Mapping Generation</strong></p>
  
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

**MA4ROM** is a multi-agent framework for relational-to-ontology (R2O) mapping generation. Given a relational database and a target ontology, MA4ROM generates class mappings, datatype property mappings, object property mappings, and executable R2RML mapping documents.

MA4ROM targets three challenges studied in the paper. First, semantic loss in column names makes datatype property mapping difficult. Second, enumerated values may correspond to ontology subclasses that are not explicit in the database schema. Third, missing foreign keys (FK) and semantic loss in relationship table names make object property mapping difficult.

To address these challenges, MA4ROM coordinates four specialized agents for automated R2O mapping generation. First, the agents collaboratively generate class, datatype property, and object property mappings. Second, MA4ROM applies a context retrieval based class and datatype property mapping algorithm to handle semantic loss in column names and enumerated value subclass mapping. Third, MA4ROM applies ontology inference and schema context based object property mapping with IND-based FK discovery to handle missing FKs and semantic loss in relationship table names.

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/553991b5-d94f-4136-ab6c-c7b8c1348171" 
    width="1000"
  />
</p>

MA4ROM follows a staged four-agent workflow:

1. **Mapping Pattern Recognition Agent** identifies mapping patterns in the source schema by combining rule-based structural analysis with LLM-based semantic classification.
2. **Class and Datatype Property Mapping Agent** generates class and datatype property mappings. It ranks ontology candidates, estimates confidence, and applies context enhancement with instance values and schema information for low-confidence mappings and enumerated value subclass mappings.
3. **Object Property Mapping Agent** maps FK columns and relationship tables to ontology object properties. It handles missing FKs with IND discovery and applies logic rules with ontology inference and schema context for object property candidate generation and selection.
4. **R2RML Mapping Generation Agent** translates the generated class, datatype property, and object property mappings into executable R2RML mapping documents.

---

# Key Contributions

- **A multi-agent framework for R2O mapping generation.** MA4ROM coordinates four specialized agents for mapping pattern recognition, class and datatype property mapping, object property mapping, and R2RML mapping generation.
- **A context retrieval algorithm for class and datatype property mapping.** The algorithm combines ontology candidate ranking, confidence estimation, and context enhancement with instance values and schema information to resolve semantic loss in column names and map enumerated values to ontology subclasses.
- **An ontology inference and schema context based object property mapping method.** MA4ROM uses IND discovery for missing FKs and logic rules over domain/range constraints and mapped table classes to generate object property candidates for FK columns and relationship tables.

---

# Experiment Results

The table below reports the main F1-score results on the RODI benchmark and the RODI-noFKs robustness setting. We include LLM4VKG as the baseline and report MA4ROM under the default setting. `RODI-noFKs` denotes the setting where all physical FKs are removed before running MA4ROM.

| Method | Rename | Restr. | Mixed | NoFK | Denorm. | Geo. | NPD | Avg. |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LLM4VKG | 0.7746 | 0.4797 | 0.7451 | 0.4615 | 0.5600 | 0.2526 | 0.3805 | 0.5220 |
| MA4ROM | 0.9367 | 0.8693 | 0.9649 | 0.8462 | 0.7734 | 0.3446 | 0.3765 | 0.7109 |
| MA4ROM RODI-noFKs | 0.8851 | 0.8825 | 0.8959 | -- | 0.5600 | 0.2885 | 0.3617 | 0.6456 |

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

The artifact uses scenarios derived from the RODI benchmark. RODI provides relational databases, target ontologies, and query pairs for evaluating relational-to-ontology mapping generation. For the full benchmark design and original datasets, please refer to the official RODI repository: https://github.com/chrpin/rodi

## RODI

The packaged RODI scenarios are placed under `dataset/`. Each scenario contains the target ontology, a PostgreSQL dump, and the query pairs used by the evaluator:

```text
dataset/<scenario>/
├── ontology.ttl
├── dump.sql
└── queries/
```

The included scenarios cover the main RODI settings used in our experiments.

| Scenario | Description |
|---|---|
| Rename | Schema names are renamed, making lexical matching harder. |
| Restructured | The relational schema structure is changed while preserving the target ontology. |
| Mixed | Multiple schema changes are combined in the same setting. |
| NoFK | Foreign keys are unavailable or incomplete in the original RODI setting. |
| Denormalized | Tables are merged or denormalized, making class and relation recovery harder. |
| Mondial | A real-world geographic database scenario. |
| NPD | A real-world petroleum-domain database scenario. |

## RODI-noFKs

We additionally provide a `RODI-noFKs` setting to evaluate robustness under missing FK constraints. In this setting, all physical FKs are removed from the database schemas, while the database contents, target ontologies, and query pairs remain unchanged. This setting tests whether MA4ROM can recover relation information through IND-based FK discovery.

# Installation and Setup

## Prerequisites

| Software | Recommended version | Purpose |
|---|---:|---|
| Python | 3.10+ | MA4ROM pipeline and evaluation scripts |
| PostgreSQL | 14+ | Loading RODI dumps and reading relational schemas |
| Java | 11+ | Ontop-based evaluation runtime |
| Git | Any recent version | Cloning the artifact repository |

The evaluation scripts use the bundled Ontop runtime under `resources/ontop/`.

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/4d220fd3-eb8b-4a83-a124-00e155c5f703" 
    width="500"
    alt="image"
  />
</p>

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

## Step 3: Validate and Load Datasets

First check that all packaged dataset files are present:

```bash
python setup_dataset.py --check
```

List all available scenarios:

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/7e8836ca-baad-4292-8546-c0996f26b851" 
    width="500"
    alt="image"
  />
</p>

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

The loader uses `PGHOST`, `PGPORT`, `PGUSER`, and `PGPASSWORD` if they are set. Otherwise, it defaults to `localhost:5432` and `postgres/postgres`.

After loading, the script prints the exact `MAMG_CURRENT_DATABASE` and, when needed, `MAMG_DB_SCHEMA` command for running MA4ROM.

## Step 4: Configure MA4ROM

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/a904067b-ceba-468b-8128-9c814c0f41b6" 
    width="420"
    alt="image"
  />
  <img 
    src="https://github.com/user-attachments/assets/020deeb0-6dbf-4cf6-a15b-f50e3e81dbc6" 
    width="420"
    alt="image"
  />
</p>

Open `ma4rom/config.py` and set the LLM configuration if you want to rerun the mapping-generation pipeline:

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/9e01c1d1-e21f-4ed4-937b-aee8b4b05d41" 
    width="500"
    alt="image"
  />
</p>

```python
LLM_API_KEY = "YOUR_API_KEY"
LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"
```

The PostgreSQL connection defaults to:

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/68e6b934-4932-47e9-ab55-774f3b020318" 
    width="500"
    alt="image"
  />
</p>

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

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/530ef757-bce0-4eab-a1b3-eeaf300bb185" 
    width="500"
    alt="image"
  />
</p>

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

# Evaluation

This repository includes generated outputs and evaluation scripts so reviewers can inspect existing results or recompute F1 scores.

## Evaluate One Result Directory

```bash
python run_my_eval.py evaluate_results/default/cmt_structured_test
```

## Evaluate All Results in a Folder

<p align="center">
  <img 
    src="https://github.com/user-attachments/assets/54a5dce4-0c21-4c51-b097-472fd1b07348" 
    width="500"
    alt="image"
  />
</p>

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

# Example Use Case

<p align="center">
  <img width="500" alt="MAMGSampleNew" src="https://github.com/user-attachments/assets/7883cc53-7ef4-4671-b741-ed416e213af6" />
</p>

# Citation

Citation information will be added after publication.
