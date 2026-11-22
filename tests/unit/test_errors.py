"""errors.py carries the stable codes and exit numbers of plan Appendix C."""

from __future__ import annotations

import pytest

from checklist_care import errors


@pytest.mark.parametrize(
    ("cls", "code", "exit_code"),
    [
        (errors.ConfigError, "CC_E_CONFIG", 2),
        (errors.DataInRepoError, "CC_E_DATA_IN_REPO", 2),
        (errors.AlreadyReviewedError, "CC_ALREADY_REVIEWED", 3),
        (errors.FetchError, "CC_E_FETCH", 4),
        (errors.ProposalError, "CC_E_PROPOSAL", 5),
        (errors.EvalAbortError, "CC_EVAL_ABORT", 6),
    ],
)
def test_appendix_c_codes_and_exits(
    cls: type[errors.CheckError], code: str, exit_code: int
) -> None:
    error = cls("something went wrong")
    assert isinstance(error, errors.CheckError)
    assert error.code == code
    assert error.exit_code == exit_code
    assert error.line() == f"{code}: something went wrong"


def test_internal_and_eval_exit_numbers() -> None:
    assert errors.EXIT_OK == 0
    assert errors.EXIT_EVAL_FAIL == 1
    assert errors.EXIT_INTERNAL == 9
    assert errors.CODE_INTERNAL == "CC_E_INTERNAL"


def test_data_in_repo_is_a_config_error() -> None:
    assert issubclass(errors.DataInRepoError, errors.ConfigError)


def test_message_line_is_always_one_line() -> None:
    error = errors.ConfigError("first\nsecond\r\n  third")
    assert error.line() == "CC_E_CONFIG: first second third"
    assert error.message == "first\nsecond\r\n  third"
