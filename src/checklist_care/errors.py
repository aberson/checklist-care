"""Stable error codes and exit numbers (plan Appendix C).

Every expected failure is a :class:`CheckError` subclass. ``cli.main`` prints
``<code>: <message>`` as one stderr line and exits with the class's
``exit_code``. Anything else is a bug and exits ``EXIT_INTERNAL``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

EXIT_OK: Final = 0
EXIT_EVAL_FAIL: Final = 1
EXIT_CONFIG: Final = 2
EXIT_ALREADY_REVIEWED: Final = 3
EXIT_FETCH: Final = 4
EXIT_PROPOSAL: Final = 5
EXIT_EVAL_ABORT: Final = 6
EXIT_INTERNAL: Final = 9

CODE_CONFIG: Final = "CC_E_CONFIG"
CODE_DATA_IN_REPO: Final = "CC_E_DATA_IN_REPO"
CODE_ALREADY_REVIEWED: Final = "CC_ALREADY_REVIEWED"
CODE_FETCH: Final = "CC_E_FETCH"
CODE_PROPOSAL: Final = "CC_E_PROPOSAL"
CODE_EVAL_ABORT: Final = "CC_EVAL_ABORT"
CODE_INTERNAL: Final = "CC_E_INTERNAL"

# HTTP API error codes (plan §6).
CODE_VALIDATION: Final = "CC_E_VALIDATION"
CODE_NOT_FOUND: Final = "CC_E_NOT_FOUND"
CODE_CONFLICT: Final = "CC_E_CONFLICT"


def one_line(text: str) -> str:
    """Collapse ``text`` onto a single line for a stderr message."""

    return " ".join(text.split())


class CheckError(Exception):
    """An expected failure with a stable code and exit number."""

    code: ClassVar[str] = CODE_INTERNAL
    exit_code: ClassVar[int] = EXIT_INTERNAL

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def line(self) -> str:
        """The one-line stderr form: ``<code>: <message>``."""

        return f"{self.code}: {one_line(self.message)}"


class ConfigError(CheckError):
    """Configuration or a startup invariant is wrong."""

    code = CODE_CONFIG
    exit_code = EXIT_CONFIG


class DataInRepoError(ConfigError):
    """The data directory sits inside a git checkout."""

    code = CODE_DATA_IN_REPO


class AlreadyReviewedError(CheckError):
    """The source was already reviewed; re-processing is a v1 non-goal."""

    code = CODE_ALREADY_REVIEWED
    exit_code = EXIT_ALREADY_REVIEWED


class FetchError(CheckError):
    """The transcript, page or text could not be obtained."""

    code = CODE_FETCH
    exit_code = EXIT_FETCH


class ProposalError(CheckError):
    """A proposal failed decoding or validation; nothing was written."""

    code = CODE_PROPOSAL
    exit_code = EXIT_PROPOSAL


@dataclass(frozen=True)
class Problem:
    """One field-level problem of a proposal, carried by :class:`ProposalInvalidError`."""

    index: int | None
    """The tip's position in ``tips``, or None for the file or the envelope."""
    field: str
    problem: str

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "field": self.field, "problem": self.problem}


class ProposalInvalidError(ProposalError):
    """The proposal failed decoding or validation; nothing was written (exit 5).

    ``propose`` prints :meth:`problems_json` on stdout and :meth:`line` on stderr.
    """

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        count = len(self.problems)
        super().__init__(
            f"the proposal has {count} problem{'' if count == 1 else 's'}, listed on stdout "
            "as JSON [{index, field, problem}]; nothing was written. Fix exactly those "
            "fields and submit again"
        )

    def problems_json(self) -> str:
        """The problems as the JSON list ``propose`` prints on stdout."""

        return json.dumps(
            [problem.to_json() for problem in self.problems], ensure_ascii=False, indent=2
        )


class EvalAbortError(CheckError):
    """An eval cannot produce a verdict."""

    code = CODE_EVAL_ABORT
    exit_code = EXIT_EVAL_ABORT


# --- HTTP API errors (plan §6) ---------------------------------------------------------


class ApiError(Exception):
    """An expected API failure: an HTTP status, its stable code, a message and the
    request fields at fault. ``server/app.py`` turns it into the §6 error body
    ``{"error": {"code", "message", "fields"}}``."""

    status: ClassVar[int] = 400
    code: ClassVar[str] = CODE_VALIDATION

    def __init__(self, message: str, fields: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.fields = tuple(fields)


class ApiValidationError(ApiError):
    """HTTP 400: the request is malformed or breaks a rule (``CC_E_VALIDATION``)."""

    status = 400
    code = CODE_VALIDATION


class ApiNotFoundError(ApiError):
    """HTTP 404: a named tip, list or source does not exist (``CC_E_NOT_FOUND``)."""

    status = 404
    code = CODE_NOT_FOUND


class ApiConflictError(ApiError):
    """HTTP 409: an explicitly requested placement is incompatible (``CC_E_CONFLICT``)."""

    status = 409
    code = CODE_CONFLICT


API_ERRORS: Final = (ApiValidationError, ApiNotFoundError, ApiConflictError)
"""The three API error statuses (plan §6), each with its code."""
