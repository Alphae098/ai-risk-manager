"""Central configuration. Everything tunable lives here or in the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MODEL_DIR = DATA_DIR / "models"
DB_PATH = Path(os.getenv("ARM_DB_PATH", DATA_DIR / "arm.db"))
RULES_PATH = Path(__file__).resolve().parent / "scoring" / "config" / "rules.yaml"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class BandConfig:
    """Score thresholds that route a transaction to a decision path.

    Anything below `approve_below` is approved outright, anything at or above
    `decline_at` is declined outright, and the span between them is the
    uncertain band that the analyst agent investigates asynchronously.
    """

    approve_below: float = 0.30
    decline_at: float = 0.85

    def band_of(self, score: float) -> str:
        if score < self.approve_below:
            return "approve"
        if score >= self.decline_at:
            return "decline"
        return "review"


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = field(default_factory=lambda: os.getenv("ARM_LLM_MODEL", "anthropic/claude-sonnet-4.5"))
    max_tool_calls: int = 6
    max_tokens: int = 1500
    temperature: float = 0.0
    timeout_s: float = 60.0

    @property
    def use_mock(self) -> bool:
        """Mock when explicitly forced, or when no key is configured."""
        if os.getenv("ARM_LLM_MOCK", "0") == "1":
            return True
        return not self.api_key


# Prices are illustrative and only used to report cost per 1,000 transactions.
PRICE_PER_1M_INPUT_USD = float(os.getenv("ARM_PRICE_IN", "3.0"))
PRICE_PER_1M_OUTPUT_USD = float(os.getenv("ARM_PRICE_OUT", "15.0"))

BANDS = BandConfig(
    approve_below=float(os.getenv("ARM_BAND_LOW", "0.30")),
    decline_at=float(os.getenv("ARM_BAND_HIGH", "0.85")),
)
LLM = LLMConfig()

# Simulated financial parameters, used by the evaluation harness.
CHARGEBACK_FEE_INR = 1500.0
FALSE_DECLINE_COST_INR = 120.0  # support + lost margin on a wrongly declined payment
