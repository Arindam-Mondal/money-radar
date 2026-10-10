"""check_dmarc on synthetic headers shaped like Gmail's (example domains, no real mail)."""

from datetime import UTC, datetime

import pytest

from app.pipeline.auth.dmarc import check_dmarc, parse_authentication_results
from app.pipeline.gmail.source import MailMessage

GMAIL_PASS = (
    "mx.google.com; "
    "dkim=pass header.i=@bank.example header.s=s1 header.b=AbC123; "
    "spf=pass (google.com: domain of alerts@bank.example designates 192.0.2.1 as permitted "
    "sender) smtp.mailfrom=alerts@bank.example; "
    "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=bank.example"
)


def _message(*headers: tuple[str, str]) -> MailMessage:
    return MailMessage(
        id="m1",
        thread_id="m1",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        headers=headers,
        text_body=None,
        html_body=None,
    )


def _from(address: str = "Bank Alerts <alerts@bank.example>") -> tuple[str, str]:
    return ("From", address)


def _auth(value: str = GMAIL_PASS) -> tuple[str, str]:
    return ("Authentication-Results", value)


# --- parse_authentication_results ---------------------------------------------------


def test_parse_extracts_authserv_and_each_method() -> None:
    authserv_id, results = parse_authentication_results(GMAIL_PASS)

    assert authserv_id == "mx.google.com"
    assert results["dmarc"] == ("pass", {"header.from": "bank.example"})
    assert results["spf"][0] == "pass"
    assert results["dkim"][1]["header.i"] == "@bank.example"


def test_parse_ignores_results_hidden_in_comments() -> None:
    _, results = parse_authentication_results(
        "mx.google.com; spf=neutral (dmarc=pass (nested) header.from=evil.example)"
    )

    assert "dmarc" not in results


def test_parse_is_case_insensitive_and_keeps_first_entry_per_method() -> None:
    _, results = parse_authentication_results(
        "MX.Google.com; DKIM=Fail header.i=@a.example; dkim=pass header.i=@b.example"
    )

    assert results["dkim"] == ("fail", {"header.i": "@a.example"})


def test_parse_handles_none_and_empty_clauses() -> None:
    authserv_id, results = parse_authentication_results("mx.google.com; none; ;")

    assert (authserv_id, results) == ("mx.google.com", {})


# --- check_dmarc ---------------------------------------------------------------------


def test_pass_when_gmail_says_pass_for_the_from_domain() -> None:
    verdict = check_dmarc(_message(_from(), _auth()))

    assert verdict.passed is True
    assert verdict.sender_domain == "bank.example"
    assert verdict.reason == "dmarc=pass"


def test_pass_when_from_is_a_subdomain_of_the_checked_domain() -> None:
    # Gmail reports the organisational domain: From alerts@mailer.bank.example
    verdict = check_dmarc(_message(_from("alerts@Mailer.Bank.example"), _auth()))

    assert verdict.passed is True
    assert verdict.sender_domain == "mailer.bank.example"


@pytest.mark.parametrize("result", ["fail", "none", "temperror", "permerror"])
def test_anything_but_pass_is_unverified(result: str) -> None:
    header = GMAIL_PASS.replace("dmarc=pass", f"dmarc={result}")

    verdict = check_dmarc(_message(_from(), _auth(header)))

    assert verdict.passed is False
    assert verdict.reason == f"dmarc={result}"


def test_missing_dmarc_result_is_unverified() -> None:
    header = "mx.google.com; spf=pass smtp.mailfrom=alerts@bank.example"

    assert check_dmarc(_message(_from(), _auth(header))).reason == "dmarc=absent"


def test_no_authentication_results_header_is_unverified() -> None:
    verdict = check_dmarc(_message(_from()))

    assert (verdict.passed, verdict.reason) == (False, "no-auth-results")


def test_only_gmails_header_counts_not_one_added_by_the_sender() -> None:
    # Gmail's own verdict (first) fails; a forged "pass" further down must not rescue it.
    gmail_fail = GMAIL_PASS.replace("dmarc=pass", "dmarc=fail")
    forged = "mx.google.com; dmarc=pass header.from=bank.example"

    verdict = check_dmarc(_message(_from(), _auth(gmail_fail), _auth(forged)))

    assert verdict.passed is False


def test_first_header_from_another_server_is_untrusted() -> None:
    header = GMAIL_PASS.replace("mx.google.com", "mail.attacker.example")

    assert check_dmarc(_message(_from(), _auth(header))).reason == "untrusted-authserv"


@pytest.mark.parametrize(
    "from_address", ["alerts@bank.example.evil.example", "alerts@notbank.example"]
)
def test_pass_for_a_different_domain_is_misaligned(from_address: str) -> None:
    verdict = check_dmarc(_message(_from(from_address), _auth()))

    assert (verdict.passed, verdict.reason) == (False, "dmarc=misaligned")


@pytest.mark.parametrize(
    "headers",
    [
        (),  # no From at all
        (_from("not-an-address"),),
        (_from("a@bank.example, b@bank.example"),),  # two addresses in one From
        (_from(), _from("other@bank.example")),  # two From headers
    ],
)
def test_unusable_from_is_unverified(headers: tuple[tuple[str, str], ...]) -> None:
    verdict = check_dmarc(_message(*headers, _auth()))

    assert (verdict.passed, verdict.sender_domain, verdict.reason) == (False, None, "bad-from")
