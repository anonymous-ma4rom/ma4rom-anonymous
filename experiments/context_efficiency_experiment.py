from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODES = ("none", "all", "confidence")
CONTEXT_EXPERIMENT_VERSION = 3
PRESET_BY_MODE = {mode: f"context_{mode}" for mode in MODES}
SETTING_LABELS = {
    "none": "Without context enhancement",
    "all": "Context for all columns",
    "confidence": "Confidence controlled context enhancement",
}
ALL_AVG_RE = re.compile(
    r"Evaluation report 'All \(AVG\)': score = "
    r"(?P<f1>[-+]?(?:\d+(?:\.\d*)?|\.\d+))"
)
AVERAGE_F1_RE = re.compile(
    r"Average F1 Score:\s*"
    r"(?P<f1>[-+]?(?:\d+(?:\.\d*)?|\.\d+))"
)


def parse_rodi_f1(report_path: Path | None) -> float | None:
    if report_path is None or not report_path.exists():
        return None
    report = report_path.read_text(encoding="utf-8", errors="replace")
    for pattern in (ALL_AVG_RE, AVERAGE_F1_RE):
        match = pattern.search(report)
        if match:
            return float(match.group("f1"))
    return None


def parse_report_args(values: list[str]) -> dict[str, Path]:
    reports = {}
    for item in values:
        mode, separator, raw_path = item.partition("=")
        if not separator or mode not in MODES:
            raise ValueError("--report 格式必须是 none|all|confidence=/path/to/rodi_report.txt")
        reports[mode] = Path(raw_path).expanduser().resolve()
    return reports


def prepare_checkpoint(database: str) -> Path:
    env = os.environ.copy()
    env.update(
        {
            "MAMG_IS_ABLATION": "true",
            "MAMG_IS_HYPER": "false",
            "MAMG_ABLATION_PRESET": "context_base",
            "MAMG_CONTEXT_PREPARE_ONLY": "true",
            "MAMG_CURRENT_DATABASE": database,
        }
    )
    subprocess.run(
        [sys.executable, "-u", str(PROJECT_ROOT / "ablation.py")],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    return PROJECT_ROOT / "output" / "ablation" / "context_base" / database


def run_setting(database: str, mode: str, checkpoint_dir: Path) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "MAMG_IS_ABLATION": "true",
            "MAMG_IS_HYPER": "false",
            "MAMG_ABLATION_PRESET": PRESET_BY_MODE[mode],
            "MAMG_CONTEXT_ENHANCEMENT_MODE": mode,
            "MAMG_CONTEXT_CHECKPOINT_DIR": str(checkpoint_dir),
            "MAMG_CURRENT_DATABASE": database,
        }
    )
    subprocess.run(
        [sys.executable, "-u", str(PROJECT_ROOT / "ablation.py")],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    summary_path = (
        PROJECT_ROOT
        / "output"
        / "ablation"
        / PRESET_BY_MODE[mode]
        / database
        / "ablation_summary.json"
    )
    return json.loads(summary_path.read_text(encoding="utf-8"))


def write_summary(database: str, summaries: dict[str, dict], reports: dict[str, Path]) -> Path:
    versions = {
        mode: summaries[mode].get("CONTEXT_EXPERIMENT_VERSION", 1)
        for mode in MODES
    }
    if any(version < CONTEXT_EXPERIMENT_VERSION for version in versions.values()):
        raise RuntimeError(
            "现有结果包含旧版或非共享 checkpoint 实验，不能用于论文。"
            "请重新运行三种 setting。"
        )
    checkpoint_ids = {
        summaries[mode].get("context_checkpoint_id")
        for mode in MODES
    }
    if None in checkpoint_ids or len(checkpoint_ids) != 1:
        raise RuntimeError(
            "三种 setting 没有使用同一份冻结 DP checkpoint，拒绝生成比较表。"
        )
    output_dir = PROJECT_ROOT / "output" / "ablation" / "context_efficiency"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{database}_context_efficiency.csv"

    fieldnames = [
        "setting",
        "mode",
        "f1",
        "llm_calls",
        "successful_responses",
        "input_tokens",
        "runtime_seconds",
        "failed_attempts",
        "common_prefix_llm_calls",
        "common_prefix_input_tokens",
        "common_prefix_runtime_seconds",
        "branch_llm_calls",
        "branch_input_tokens",
        "branch_runtime_seconds",
        "context_llm_calls",
        "context_input_tokens",
        "context_runtime_seconds",
        "selected_tables",
        "selected_columns",
        "checkpoint_id",
        "mapping_path",
        "rodi_report",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for mode in MODES:
            summary = summaries[mode]
            context = summary.get("context_efficiency", {})
            pipeline = summary.get("pipeline_llm_metrics", {})
            prefix = summary.get("common_prefix_llm_metrics", {})
            branch = summary.get("branch_llm_metrics", {})
            report = reports.get(mode)
            writer.writerow(
                {
                    "setting": SETTING_LABELS[mode],
                    "mode": mode,
                    "f1": parse_rodi_f1(report),
                    "llm_calls": pipeline.get("api_attempts", 0),
                    "successful_responses": pipeline.get("llm_calls", 0),
                    "input_tokens": pipeline.get("input_tokens", 0),
                    "runtime_seconds": summary.get("elapsed_seconds"),
                    "failed_attempts": pipeline.get("failed_attempts", 0),
                    "common_prefix_llm_calls": prefix.get("api_attempts", 0),
                    "common_prefix_input_tokens": prefix.get("input_tokens", 0),
                    "common_prefix_runtime_seconds": summary.get(
                        "common_prefix_elapsed_seconds",
                        0,
                    ),
                    "branch_llm_calls": branch.get("api_attempts", 0),
                    "branch_input_tokens": branch.get("input_tokens", 0),
                    "branch_runtime_seconds": summary.get("branch_elapsed_seconds", 0),
                    "context_llm_calls": context.get("api_attempts", 0),
                    "context_input_tokens": context.get("input_tokens", 0),
                    "context_runtime_seconds": context.get("runtime_seconds", 0),
                    "selected_tables": context.get("selected_tables", 0),
                    "selected_columns": context.get("selected_columns", 0),
                    "checkpoint_id": summary.get("context_checkpoint_id"),
                    "mapping_path": summary.get("output_mapping"),
                    "rodi_report": str(report) if report else "",
                }
            )
    return csv_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the three Class/DP context-efficiency ablation settings."
    )
    parser.add_argument("--database", default="cmt_structured")
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        help="Optional RODI report: confidence=/path/to/report.txt",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Do not rerun mappings; rebuild CSV from existing ablation summaries.",
    )
    parser.add_argument(
        "--run-mode",
        action="append",
        choices=MODES,
        help="Only rerun the selected mode; other modes reuse existing summaries.",
    )
    parser.add_argument(
        "--reuse-checkpoint",
        action="store_true",
        help="Reuse output/ablation/context_base/<database> instead of rerunning the common prefix.",
    )
    args = parser.parse_args()
    reports = parse_report_args(args.report)

    summaries = {}
    checkpoint_dir = None
    if not args.summarize_only:
        if args.reuse_checkpoint:
            checkpoint_dir = (
                PROJECT_ROOT
                / "output"
                / "ablation"
                / "context_base"
                / args.database
            )
            if not (checkpoint_dir / "context_prefix_summary.json").exists():
                raise FileNotFoundError(f"冻结 checkpoint 不存在: {checkpoint_dir}")
        else:
            checkpoint_dir = prepare_checkpoint(args.database)
    for mode in MODES:
        summary_path = (
            PROJECT_ROOT
            / "output"
            / "ablation"
            / PRESET_BY_MODE[mode]
            / args.database
            / "ablation_summary.json"
        )
        should_run = not args.summarize_only and (
            not args.run_mode or mode in args.run_mode
        )
        if should_run:
            summaries[mode] = run_setting(args.database, mode, checkpoint_dir)
        else:
            summaries[mode] = json.loads(summary_path.read_text(encoding="utf-8"))

    csv_path = write_summary(args.database, summaries, reports)
    print(f"\nContext-efficiency summary: {csv_path}")


if __name__ == "__main__":
    main()
