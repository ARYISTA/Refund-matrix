"""Policy engine output. Persisted with the case and shown to reviewers and customers."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.enums import PolicyDecision, RuleName
from app.contracts.money import CURRENCY, Currency, Paise


class RuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: RuleName
    passed: bool
    # Customer-visible (the policy card is shown in the chat panel). Write it as a plain
    # sentence and never reveal internal signals such as the fraud flag.
    reason: str = Field(min_length=1, max_length=300)


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    rules: tuple[RuleResult, ...] = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    amount_paise: Paise
    currency: Currency = CURRENCY

    @model_validator(mode="after")
    def _check_consistency(self) -> "PolicyResult":
        names = [r.rule for r in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("each rule may appear only once")
        all_passed = all(r.passed for r in self.rules)
        if self.decision is PolicyDecision.AUTO_APPROVE and not all_passed:
            raise ValueError("AUTO_APPROVE requires every rule to pass")
        if self.decision is not PolicyDecision.AUTO_APPROVE and all_passed:
            raise ValueError(f"{self.decision.value} requires at least one failed rule")
        return self
