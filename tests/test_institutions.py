"""Institution registry invariants and config precedence.

The precedence rule (explicit env override beats the profile) is load-bearing:
it is what lets a school with no registry entry work without a code change.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from avenue_mcp.config import Settings
from avenue_mcp.errors import ConfigError
from avenue_mcp.institutions import (
    CARLETON,
    INSTITUTIONS,
    MCMASTER,
    custom_institution,
    get_institution,
)


class TestRegistry:
    def test_keys_match_ids(self):
        for key, inst in INSTITUTIONS.items():
            assert key == inst.id
            assert key == key.lower()

    def test_urls_normalized(self):
        # The base_url field_validator does NOT re-run when the model validator
        # assigns from a profile, so the registry must already be normalized.
        for inst in INSTITUTIONS.values():
            assert inst.base_url.startswith("https://")
            assert not inst.base_url.endswith("/")
            assert inst.login_url.startswith("https://")

    def test_timezones_resolve(self):
        # Windows ships no system tz database; this would fail without tzdata.
        for inst in INSTITUTIONS.values():
            assert ZoneInfo(inst.timezone) is not None

    def test_unknown_id_lists_valid_ids(self):
        with pytest.raises(ValueError) as exc:
            get_institution("uoft")
        message = str(exc.value)
        assert "carleton" in message and "mcmaster" in message

    def test_lookup_is_case_insensitive(self):
        assert get_institution("CARLETON") is CARLETON


class TestNaming:
    """Carleton's Brightspace is not called Avenue. That name is McMaster's."""

    def test_mcmaster_keeps_its_brand(self):
        assert MCMASTER.lms_name == "Avenue to Learn"
        assert MCMASTER.credential_brand == "MacID"

    def test_carleton_is_never_called_avenue(self):
        assert "Avenue" not in CARLETON.lms_name
        assert "Avenue" not in CARLETON.display
        assert CARLETON.lms_name == "Brightspace"
        assert CARLETON.credential_brand == "MyCarletonOne"


class TestCarletonLoginUrl:
    def test_login_url_is_the_saml_initiator(self):
        # Regression guard. https://brightspace.carleton.ca/ and /d2l/login both
        # serve a LOCAL D2L username/password box that MyCarletonOne credentials
        # do not work in, and /d2l/lp/auth/saml/init 404s. Only this path 302s to
        # the ADFS IdP, so pointing login_url anywhere else strands the user on a
        # form their credentials are rejected by.
        assert CARLETON.login_url.endswith("/d2l/lp/auth/saml/login")

    def test_single_host(self):
        # Unlike McMaster, login and API share one host here.
        assert CARLETON.login_url.startswith(CARLETON.base_url)


class TestCapabilities:
    def test_both_instances_are_probed(self):
        assert MCMASTER.verified is True
        assert CARLETON.verified is True

    def test_instructor_scope_routes_denied_at_both(self):
        # All four agree across two instances on the same Brightspace version,
        # which is why these read as D2L role defaults rather than per-school
        # configuration. mysubmissions belongs here despite a stray folder on the
        # probed Carleton course returning 200 -- 13 of 14 folders 403. See
        # docs/09.
        for inst in (MCMASTER, CARLETON):
            assert inst.capability("course_details") == "denied"
            assert inst.capability("dropbox_mysubmissions") == "denied"
            assert inst.capability("quiz_attempts") == "denied"
            assert inst.capability("orgunit_users") == "denied"

    def test_classlist_readable_at_both(self):
        # Predicted 403 in docs/02 at both. Measured 200 at both. The roster is
        # withheld by get_class_list on FIPPA grounds, not by the API.
        for inst in (MCMASTER, CARLETON):
            assert inst.capability("classlist") == "permitted"

    def test_unknown_key_defaults_to_unverified(self):
        assert MCMASTER.capability("some_future_route") == "unverified"
        # discussion_posts was never reached at Carleton -- the probed course had
        # forums but no topics -- so it must NOT read as permitted.
        assert CARLETON.capability("discussion_posts") == "unverified"


class TestPrecedence:
    def test_default_is_mcmaster(self, monkeypatch):
        monkeypatch.delenv("AVENUE_MCP_INSTITUTION", raising=False)
        s = Settings()
        assert s.institution_profile.id == "mcmaster"
        assert s.base_url == MCMASTER.base_url
        assert s.login_url == MCMASTER.login_url

    def test_selecting_carleton_applies_the_whole_profile(self, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        s = Settings()
        assert s.base_url == "https://brightspace.carleton.ca"
        assert s.login_url.endswith("/d2l/lp/auth/saml/login")
        assert s.timezone == "America/Toronto"
        assert s.institution_profile.lms_name == "Brightspace"

    def test_explicit_base_url_wins_over_profile(self, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        monkeypatch.setenv("AVENUE_MCP_BASE_URL", "https://learn.example.edu")
        s = Settings()
        assert s.base_url == "https://learn.example.edu"

    def test_override_degrades_to_an_unverified_custom_profile(self, monkeypatch):
        # Pointing base_url at a school the profile does not describe must NOT
        # inherit that profile's name or -- far worse -- its measured
        # capabilities. That would assert another school's permissions as fact.
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "mcmaster")
        monkeypatch.setenv("AVENUE_MCP_BASE_URL", "https://learn.example.edu")
        s = Settings()
        prof = s.institution_profile
        assert prof.id != "mcmaster"
        assert prof.lms_name == "Brightspace"
        assert prof.verified is False
        assert prof.capability("dropbox_mysubmissions") == "unverified"

    def test_trailing_slash_stripped_on_override(self, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_BASE_URL", "https://learn.example.edu/")
        assert Settings().base_url == "https://learn.example.edu"

    def test_unknown_institution_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "hogwarts")
        with pytest.raises(ConfigError):
            Settings()

    def test_matching_base_url_keeps_the_real_profile(self, monkeypatch):
        # Explicitly setting base_url to the profile's own value is not a
        # different school, so the profile must survive intact.
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        monkeypatch.setenv("AVENUE_MCP_BASE_URL", CARLETON.base_url)
        assert Settings().institution_profile.id == "carleton"


class TestCustomInstitution:
    def test_is_unbranded_and_unverified(self):
        prof = custom_institution("https://lms.someschool.ca/")
        assert prof.base_url == "https://lms.someschool.ca"
        assert prof.verified is False
        assert prof.capabilities == {}
        assert "Avenue" not in prof.display


class TestStateNamespacing:
    def test_paths_live_under_the_institution(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        s = Settings()
        assert s.institution_dir == tmp_path / "carleton"
        assert s.session_path == tmp_path / "carleton" / "session.json"
        assert s.index_path == tmp_path / "carleton" / "index.db"
        assert s.log_dir == tmp_path / "carleton" / "logs"
        assert s.render_dir == tmp_path / "carleton" / "cache" / "renders"

    def test_two_schools_never_collide(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "mcmaster")
        mac = Settings()
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        car = Settings()
        # org_unit_ids are per-instance and WILL collide, so a shared index
        # would silently overwrite one school's course with another's.
        assert mac.index_path != car.index_path
        assert mac.session_path != car.session_path


class TestLegacyAdoption:
    def _legacy(self, tmp_path):
        (tmp_path / "session.json").write_text('{"cookies": []}', encoding="utf-8")
        (tmp_path / "index.db").write_text("db", encoding="utf-8")

    def test_flat_state_is_moved_into_mcmaster(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "mcmaster")
        self._legacy(tmp_path)

        s = Settings()
        s.ensure_dirs()

        assert s.session_path.read_text(encoding="utf-8") == '{"cookies": []}'
        assert s.index_path.read_text(encoding="utf-8") == "db"
        # Moved, not copied: a second copy of a live credential at a path
        # nothing will subsequently chmod or ACL is a security regression.
        assert not (tmp_path / "session.json").exists()
        assert not (tmp_path / "index.db").exists()

    def test_is_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "mcmaster")
        self._legacy(tmp_path)
        s = Settings()
        s.ensure_dirs()
        s.ensure_dirs()
        assert s.session_path.read_text(encoding="utf-8") == '{"cookies": []}'

    def test_never_adopts_into_another_school(self, monkeypatch, tmp_path):
        # Legacy flat state was unambiguously McMaster's. Adopting it into
        # carleton/ would hand a Carleton session McMaster's cookies.
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "carleton")
        self._legacy(tmp_path)

        s = Settings()
        s.ensure_dirs()

        assert not s.session_path.exists()
        assert (tmp_path / "session.json").exists()
