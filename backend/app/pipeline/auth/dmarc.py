"""DMARC check (ING-5): trust only Gmail's own Authentication-Results verdict."""

import re
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Final

from app.pipeline.gmail.source import MailMessage

# The receiving server whose verdict we trust. Any other Authentication-Results header
# was written by someone upstream (possibly the sender) and proves nothing.
TRUSTED_AUTHSERV_ID: Final = "mx.google.com"

_COMMENT = re.compile(r"\([^()]*\)")  # one level of (...); applied until none are left

# method -> (result, {property: value}), e.g. "dmarc" -> ("pass", {"header.from": "bank.in"})
AuthResults = dict[str, tuple[str, dict[str, str]]]


@dataclass(frozen=True, slots=True)
class AuthVerdict:
    passed: bool
    sender_domain: str | None  # From address domain, lowercase; None if From is unusable
    reason: str  # short code stored in messages.auth_result, e.g. "dmarc=pass"


def check_dmarc(message: MailMessage) -> AuthVerdict:
    """Pass only if Gmail says dmarc=pass for a domain the From address belongs to."""
    sender_domain = from_domain(message)
    if sender_domain is None:
        return AuthVerdict(passed=False, sender_domain=None, reason="bad-from")

    headers = message.header_all("Authentication-Results")
    if not headers:
        return AuthVerdict(passed=False, sender_domain=sender_domain, reason="no-auth-results")

    # Gmail prepends its header, so the first one is Gmail's; later ones came with the mail.
    authserv_id, results = parse_authentication_results(headers[0])
    if authserv_id != TRUSTED_AUTHSERV_ID:
        return AuthVerdict(passed=False, sender_domain=sender_domain, reason="untrusted-authserv")

    dmarc = results.get("dmarc")
    if dmarc is None:
        return AuthVerdict(passed=False, sender_domain=sender_domain, reason="dmarc=absent")
    result, properties = dmarc
    if result != "pass":
        return AuthVerdict(passed=False, sender_domain=sender_domain, reason=f"dmarc={result}")

    if not _aligned(sender_domain, properties.get("header.from", "")):
        return AuthVerdict(passed=False, sender_domain=sender_domain, reason="dmarc=misaligned")
    return AuthVerdict(passed=True, sender_domain=sender_domain, reason="dmarc=pass")


def parse_authentication_results(value: str) -> tuple[str, AuthResults]:
    """Parse an RFC 8601 header: 'authserv-id; method=result prop=value ...; ...'.

    Comments in parentheses are removed first, so text inside them (which the sending
    side can influence) can never be mistaken for a result. The first entry per method
    wins (there can be several dkim=… entries, one per signature).
    """
    text = _strip_comments(value)
    authserv, *clauses = text.split(";")
    authserv_tokens = authserv.split()
    authserv_id = authserv_tokens[0].lower() if authserv_tokens else ""

    results: AuthResults = {}
    for clause in clauses:
        tokens = clause.split()
        if not tokens or "=" not in tokens[0]:
            continue  # e.g. "none": no authentication was performed
        method, _, result = tokens[0].partition("=")
        properties = {
            key.lower(): val for key, _, val in (t.partition("=") for t in tokens[1:]) if val
        }
        results.setdefault(method.lower(), (result.lower(), properties))
    return authserv_id, results


def from_domain(message: MailMessage) -> str | None:
    """Domain of the single From address, lowercase; None if missing, multiple or malformed."""
    addresses = getaddresses(message.header_all("From"))
    if len(addresses) != 1:
        return None
    local, at, domain = addresses[0][1].rpartition("@")
    if not (local and at and domain):
        return None
    return domain.lower().rstrip(".")


def _strip_comments(value: str) -> str:
    previous = None
    while previous != value:  # repeat to peel nested comments from the inside out
        previous, value = value, _COMMENT.sub(" ", value)
    return value


def _aligned(sender_domain: str, policy_domain: str) -> bool:
    """DMARC relaxed alignment: From domain equals the checked domain or is a subdomain."""
    policy = policy_domain.lower().rstrip(".")
    return bool(policy) and (sender_domain == policy or sender_domain.endswith("." + policy))
