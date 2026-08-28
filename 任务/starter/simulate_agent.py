from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_interface import Agent


PACKAGE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PACKAGE_DIR / "data"
OUTPUT_DIR = PACKAGE_DIR / "outputs"
REQUIRED_KEYS = {"instruction", "purchased_product_id", "trace", "summary"}


def read_jsonl(path: Path) -> list[dict]:
    # Minimal JSONL loader used by the simulation harness.
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def validate_result(result: dict) -> list[str]:
    # The harness checks output shape only. It does not judge the internal method.
    errors = []
    if not isinstance(result, dict):
        return ["result must be a dict"]
    missing = sorted(REQUIRED_KEYS - set(result))
    if missing:
        errors.append(f"missing keys: {missing}")
    if "trace" in result and not isinstance(result["trace"], list):
        errors.append("trace must be a list")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", default=str(DATA_DIR / "tasks.jsonl"))
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", default=str(OUTPUT_DIR / "agent_simulation.jsonl"))
    args = parser.parse_args()

    # Load public cases and call the submitted interface once per case.
    tasks = read_jsonl(Path(args.tasks))[: args.limit]
    agent = Agent(DATA_DIR)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", encoding="utf-8") as f:
        for task in tasks:
            try:
                # The complete agent workflow must happen inside Agent.run.
                result = agent.run(task["instruction"])
                errors = validate_result(result)
            except NotImplementedError as exc:
                result = {"instruction": task["instruction"], "error": str(exc)}
                errors = ["Agent.run is not implemented"]
            row = {"task_id": task["task_id"], "validation_errors": errors, **result}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(out)


if __name__ == "__main__":
    main()
