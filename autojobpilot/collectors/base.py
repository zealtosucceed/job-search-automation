"""Collector interface — every source is source-agnostic downstream."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config
from ..models import Job


class Collector(ABC):
    name: str = "base"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @abstractmethod
    def collect(self) -> list[Job]:
        """Fetch and normalize jobs from this source. Must not raise on a single
        bad page — log and continue, returning whatever was collected."""
