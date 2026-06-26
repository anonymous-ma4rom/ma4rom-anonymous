import psycopg2
import json

from config import DB_CONFIG, DB_SCHEMA_NAME          # 统一从 config.py 读取

# DB_CONFIG 由 config.py 提供，此处直接 re-export 方便其他模块 `from utils.db_utils import DB_CONFIG`
__all__ = ["DB_CONFIG", "get_connection", "get_conn", "read_schema",
           "fetch_sample_rows", "fetch_col_data_as_set", "get_table_row_count"]


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


# 历史别名，兼容仍使用 get_conn 名称的调用方
get_conn = get_connection


def get_tables(cur, schema_name: str = DB_SCHEMA_NAME):
    """
    通过 PostgreSQL 官方目录 pg_catalog 获取指定 schema 下的普通表。
    """
    cur.execute(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind = 'r'
        ORDER BY c.relname;
        """,
        (schema_name,),
    )
    return [row[0] for row in cur.fetchall()]


def get_columns(cur, table, schema_name: str = DB_SCHEMA_NAME):
    """
    通过 pg_attribute + format_type 获取列名和类型（按物理列序）。
    """
    cur.execute(
        """
        SELECT a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum;
        """,
        (schema_name, table),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def get_primary_keys(cur, table, schema_name: str = DB_SCHEMA_NAME):
    """
    通过 pg_constraint 获取主键列（保留列序）。
    """
    cur.execute(
        """
        SELECT a.attname
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        WHERE con.contype = 'p'
          AND ns.nspname = %s
          AND cls.relname = %s
        ORDER BY k.ord;
        """,
        (schema_name, table),
    )
    return [row[0] for row in cur.fetchall()]


def get_foreign_keys(cur, table, schema_name: str = DB_SCHEMA_NAME):
    """
    通过 pg_constraint 获取外键列映射（严格按复合 FK 列序一一对应）。
    """
    cur.execute(
        """
        SELECT con.conname AS constraint_name,
               array_length(con.conkey, 1) AS fk_arity,
               src.attname AS column_name,
               refcls.relname AS foreign_table,
               tgt.attname AS foreign_column
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        JOIN pg_class refcls ON refcls.oid = con.confrelid
        JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS s(attnum, ord) ON true
        JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS t(attnum, ord) ON t.ord = s.ord
        JOIN pg_attribute src ON src.attrelid = con.conrelid AND src.attnum = s.attnum
        JOIN pg_attribute tgt ON tgt.attrelid = con.confrelid AND tgt.attnum = t.attnum
        WHERE con.contype = 'f'
          AND ns.nspname = %s
          AND cls.relname = %s
        ORDER BY con.conname, s.ord;
        """,
        (schema_name, table),
    )

    foreign_keys = []
    for row in cur.fetchall():
        foreign_keys.append({
            "constraint_name": row[0],
            "fk_arity": row[1],
            "column": row[2],
            "references_table": row[3],
            "references_column": row[4]
        })
    return foreign_keys


def read_schema():
    conn = get_connection()
    cur = conn.cursor()

    schema = {}
    tables = get_tables(cur, DB_SCHEMA_NAME)

    for table in tables:
        schema[table] = {
            "columns": get_columns(cur, table, DB_SCHEMA_NAME),
            "primary_key": get_primary_keys(cur, table, DB_SCHEMA_NAME),
            "foreign_keys": get_foreign_keys(cur, table, DB_SCHEMA_NAME)
        }

    cur.close()
    conn.close()

    return schema


def fetch_sample_rows(table_name: str, limit: int = 5) -> list:
    """
    从指定表拉取样本行，返回 list[dict]。
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT * FROM "{table_name}" LIMIT %s', (limit,))
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        print(f"  [WARN] 拉取 {table_name} 数据失败: {e}")
        conn.rollback()
        return []
    finally:
        cur.close()
        conn.close()


def fetch_col_data_as_set(conn, schema_name: str, table: str, column: str) -> set:
    """
    执行 SQL 去重查询，将一列的真实数据拉入内存，返回 Set。
    """
    query = (
        f'SELECT DISTINCT "{column}" '
        f'FROM "{schema_name}"."{table}" '
        f'WHERE "{column}" IS NOT NULL;'
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"读取 {table}.{column} 失败: {e}")
        conn.rollback()
        return set()


def get_table_row_count(conn, table_name: str) -> int:
    """查询表的非空行数。失败时返回 0。"""
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{table_name}";')
            return cur.fetchone()[0]
    except Exception as e:
        print(f"  [WARN] 查询 {table_name} 行数失败: {e}")
        conn.rollback()
        return 0


if __name__ == "__main__":
    schema = read_schema()
    print(json.dumps(schema, indent=4))
