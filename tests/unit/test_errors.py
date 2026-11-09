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


@pytest.mark.parametrize(
    ("cls", "status", "code"),
    [
        (errors.ApiValidationError, 400, "CC_E_VALIDATION"),
        (errors.ApiNotFoundError, 404, "CC_E_NOT_FOUND"),
        (errors.ApiConflictError, 409, "CC_E_CONFLICT"),
    ],
)
def test_api_errors_carry_the_plan_section_6_status_and_code(
    cls: type[errors.ApiError], status: int, code: str
) -> None:
    error = cls("list 3 is a kids list", ["list_ids"])
    assert isinstance(error, errors.ApiError)
    assert (error.status, error.code) == (status, code)
    assert error.message == "list 3 is a kids list"
    assert error.fields == ("list_ids",)
    assert cls in errors.API_ERRORS


def test_the_api_has_exactly_three_error_statuses() -> None:
    assert {cls.status: cls.code for cls in errors.API_ERRORS} == {
        400: errors.CODE_VALIDATION,
        404: errors.CODE_NOT_FOUND,
        409: errors.CODE_CONFLICT,
    }
