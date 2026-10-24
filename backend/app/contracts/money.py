"""Money primitives. All amounts are integer paise (1 INR = 100 paise). No floats, ever.

`StrictInt` rejects floats (even 5.0) and bools, so a float cannot sneak in from JSON,
YAML or an LLM response and be silently truncated.
"""

from typing import Annotated, Literal

from pydantic import Field, StrictInt

Currency = Literal["INR"]
CURRENCY: Currency = "INR"

# >= 0. Use for amounts that may legitimately be zero (e.g. nothing refunded yet).
Paise = Annotated[StrictInt, Field(ge=0)]
# > 0. Use for amounts that must move money (refund amount, payout amount, thresholds).
PositivePaise = Annotated[StrictInt, Field(gt=0)]
