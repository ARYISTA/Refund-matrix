"""PolicyConfig: the validated shape of policy.yaml. The engine (Phase 2) reads this."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

from app.contracts.enums import ReasonCategory
from app.contracts.money import Currency, PositivePaise

DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.yaml"


class PolicyConfigError(RuntimeError):
    """Raised at startup when policy.yaml is missing or invalid. The app must not boot."""


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    currency: Currency
    auto_approve_below_paise: PositivePaise
    return_window_days: Annotated[StrictInt, Field(gt=0)]
    eligible_reasons: tuple[ReasonCategory, ...]
    max_refunds_per_customer_30d: Annotated[StrictInt, Field(ge=0)]
    escalate_at_or_above_threshold: StrictBool

    @field_validator("eligible_reasons")
    @classmethod
    def _validate_reasons(cls, v: tuple[ReasonCategory, ...]) -> tuple[ReasonCategory, ...]:
        if not v:
            raise ValueError("eligible_reasons must not be empty")
        if len(set(v)) != len(v):
            raise ValueError("eligible_reasons must not contain duplicates")
        if ReasonCategory.OTHER in v:
            raise ValueError("'other' is the catch-all for ineligible reasons and cannot be eligible")
        return v


def load_policy(path: Path | str | None = None) -> PolicyConfig:
    """Load and validate policy.yaml. Raises PolicyConfigError with a readable message."""
    policy_path = Path(path) if path is not None else DEFAULT_POLICY_PATH
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyConfigError(f"Policy file not found: {policy_path}") from exc
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"Policy file is not valid YAML: {policy_path}") from exc

    if not isinstance(raw, dict):
        raise PolicyConfigError(f"Policy file must contain a mapping of settings: {policy_path}")

    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise PolicyConfigError(f"Policy file failed validation ({policy_path}):\n{exc}") from exc
