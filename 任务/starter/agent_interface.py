from __future__ import annotations

from pathlib import Path


class Agent:
    """Agent task interface.

    Keep this public calling convention:

        agent = Agent(data_dir)
        result = agent.run(instruction)
    """

    def __init__(self, data_dir: str | Path):
        # Root directory of the provided dataset. Implementations may load
        # products, prompts, indexes, caches, or model configuration from here.
        self.data_dir = Path(data_dir)

    def run(self, instruction: str) -> dict:
        """Run the complete agent workflow for one shopping instruction.

        Return at least:
            instruction: str
            purchased_product_id: str | None
            trace: list
            summary: str
        """
        raise NotImplementedError("Please implement Agent.run(instruction).")
