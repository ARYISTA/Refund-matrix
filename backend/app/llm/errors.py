"""LLM errors. Messages are short and never contain prompts, customer text or model output.

LLMError
├── LLMConfigError          missing key or model name, unsupported provider
├── LLMRequestError         the request itself was rejected (4xx other than auth); a bug
├── LLMInvalidOutputError   the model's output failed validation (retry once, then a human)
├── LLMToolLoopError        the model kept calling tools instead of answering
└── LLMProviderError        the provider failed; the Phase 10 fallback chain catches this
    ├── LLMTimeoutError
    ├── LLMRateLimitError
    ├── LLMUnavailableError
    └── LLMAuthError
"""


class LLMError(Exception):
    #: Whether the retry layer may try the same call again.
    retryable: bool = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        #: Seconds the provider asked us to wait (Retry-After), if it said so.
        self.retry_after = retry_after


class LLMConfigError(LLMError):
    pass


class LLMRequestError(LLMError):
    pass


class LLMInvalidOutputError(LLMError):
    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        #: Field-level problems, safe to show the model on the corrective retry.
        self.problems = problems


class LLMToolLoopError(LLMError):
    pass


class LLMProviderError(LLMError):
    pass


class LLMTimeoutError(LLMProviderError):
    retryable = True


class LLMRateLimitError(LLMProviderError):
    retryable = True


class LLMUnavailableError(LLMProviderError):
    retryable = True


class LLMAuthError(LLMProviderError):
    retryable = False
