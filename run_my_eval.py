"""
在项目根目录运行：

单目录评测：
    python run_my_eval.py outputs/npd_atomic_tests_mytest

批量评测某个父目录下一层子目录：
    python run_my_eval.py outputs/abltions/cea --batch
"""
import argparse
import os
import re
import sys
from typing import Iterable

sys.path.insert(0, ".")

from src.vkg_utils.evaluate_pipeline import run_evaluation_pipeline


ONTOLOGY_PROPERTIES_NAME = "ontology.properties"


def parse_args():
    parser = argparse.ArgumentParser(description="自动评测单个结果目录或批量评测多个结果目录。")
    parser.add_argument(
        "target_path",
        help="待评测目录。单目录模式传具体结果目录；批量模式传父目录。",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="开启后遍历 target_path 下一层子目录并逐个评测。",
    )
    parser.add_argument(
        "--skip-done",
        action="store_true",
        help="批量模式下如果子目录已存在 f1.txt，则跳过。",
    )
    return parser.parse_args()


def read_schema_from_properties(property_file: str) -> str:
    with open(property_file, "r", encoding="utf-8") as f:
        content = f.read()

    default_schema_match = re.search(r"jdbc\.defaultSchema\s*=\s*([^\s]+)", content)
    if default_schema_match:
        return default_schema_match.group(1).strip()

    url_schema_match = re.search(r"currentSchema=([^&\s]+)", content)
    if url_schema_match:
        return url_schema_match.group(1).strip()

    raise ValueError(f"无法从 {property_file} 解析 schema。")


def build_eval_paths(base_path: str, test_name: str) -> dict[str, str]:
    ttl_mapping_file = os.path.join(base_path, f"rodi_{test_name}_generated_mapping.ttl")
    ontology_file = os.path.join(base_path, f"rodi_{test_name}_generated_ontology.ttl")
    property_file = os.path.join(base_path, ONTOLOGY_PROPERTIES_NAME)
    qpair_folder = os.path.join("dataset", test_name, "queries")
    output_metrics_path = os.path.join(base_path, "metrics_details.json")
    output_f1_path = os.path.join(base_path, "f1.txt")

    return {
        "ttl_mapping_file": ttl_mapping_file,
        "ontology_file": ontology_file,
        "property_file": property_file,
        "qpair_folder": qpair_folder,
        "output_metrics_path": output_metrics_path,
        "output_f1_path": output_f1_path,
    }


def validate_inputs(paths: dict[str, str]) -> None:
    required_files = [
        paths["ttl_mapping_file"],
        paths["ontology_file"],
        paths["property_file"],
    ]
    for file_path in required_files:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"缺少文件: {file_path}")

    if not os.path.exists(paths["qpair_folder"]):
        raise FileNotFoundError(f"缺少 query 目录: {paths['qpair_folder']}")


def evaluate_directory(base_path: str) -> None:
    property_file = os.path.join(base_path, ONTOLOGY_PROPERTIES_NAME)
    if not os.path.exists(property_file):
        raise FileNotFoundError(f"缺少文件: {property_file}")

    test_name = read_schema_from_properties(property_file)
    paths = build_eval_paths(base_path, test_name)
    validate_inputs(paths)

    print(f"✓ 开始评估: {base_path}")
    print(f"  使用 schema/test_name: {test_name}")

    run_evaluation_pipeline(
        ttl_mapping_file=paths["ttl_mapping_file"],
        ontology_file=paths["ontology_file"],
        property_file=paths["property_file"],
        qpair_folder=paths["qpair_folder"],
        dbname=test_name,
        ontop_cli_path=os.path.abspath("./resources/ontop"),
        output_metrics_path=paths["output_metrics_path"],
        output_f1_path=paths["output_f1_path"],
    )

    print(f"✓ 评估完成: {base_path}")
    print(f"  F1 分数: {paths['output_f1_path']}")
    print(f"  详细指标: {paths['output_metrics_path']}")


def iter_subdirectories(parent_dir: str) -> Iterable[str]:
    for name in sorted(os.listdir(parent_dir)):
        if name.startswith("."):
            continue
        full_path = os.path.join(parent_dir, name)
        if os.path.isdir(full_path):
            yield full_path


def evaluate_batch(parent_dir: str, skip_done: bool) -> None:
    subdirs = list(iter_subdirectories(parent_dir))
    if not subdirs:
        raise FileNotFoundError(f"目录下没有可评测的子目录: {parent_dir}")

    for subdir in subdirs:
        output_f1_path = os.path.join(subdir, "f1.txt")
        if skip_done and os.path.exists(output_f1_path):
            print(f"⏭ 跳过已完成目录: {subdir}")
            continue

        try:
            evaluate_directory(subdir)
        except Exception as exc:
            print(f"❌ 评估失败: {subdir}")
            print(f"  原因: {exc}")


def main():
    args = parse_args()
    target_path = os.path.abspath(args.target_path)

    if not os.path.isdir(target_path):
        print(f"❌ 目录不存在: {target_path}")
        sys.exit(1)

    if args.batch:
        evaluate_batch(target_path, args.skip_done)
    else:
        evaluate_directory(target_path)


if __name__ == "__main__":
    main()
