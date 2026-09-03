"""Console logger backed by rich."""
from __future__ import annotations

from rich.console import Console

console = Console(stderr=True)


def log(msg: str) -> None:
    console.log(msg)
