db_config = {
    "user": "postgres",
    "password": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "rodi"   # ← 确保有这一行
}

api_names = ["gpt_4o_mini"]

subset_names = ["conference_naive"]

# =========================
# Data Enrichment config
# =========================
DATA_ENRICHMENT_CONFIG = {
    "enable_enrichment": False
}

# =========================
# API config path
# =========================
API_CONFIG_PATH = "resources/ampi.json"

DEFAULT_API_ID = "gpt-4o-mini"