"""Region pack registry and the REGION_PACKS setting."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.regions import load_region_packs


def test_load_known_pack() -> None:
    (india,) = load_region_packs(["india"])

    assert india.code == "india"
    assert india.currencies == ("INR",)


def test_unknown_pack_fails_loudly() -> None:
    with pytest.raises(ValueError, match=r"unknown region pack\(s\) \['mars'\]"):
        load_region_packs(["india", "mars"])


def test_region_packs_defaults_to_india() -> None:
    assert Settings().region_packs == ("india",)


def test_region_packs_parses_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGION_PACKS", " India, us ,")

    assert Settings().region_packs == ("india", "us")


def test_region_packs_empty_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGION_PACKS", " , ")

    with pytest.raises(ValidationError, match="at least one pack"):
        Settings()
