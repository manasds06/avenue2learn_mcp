"""Per-institution Brightspace profiles.

Every school runs the same D2L Valence API, so the client and tool layers are
institution-agnostic. What differs is the host, the SSO entry point, what the
school *calls* its Brightspace instance, and which routes a student account is
actually permitted to reach.

McMaster brands its instance "Avenue to Learn". That name is McMaster's alone --
Carleton's is just "Brightspace". Calling Carleton's LMS "Avenue" in text a
student reads is simply wrong, which is why `lms_name` lives here.

`capabilities` records what an authenticated probe MEASURED, never what the
Valence docs predict. An unprobed instance reports "unverified" for everything,
and the tool layer says so rather than borrowing another school's results.

This module is a leaf: stdlib only. In particular it does NOT import
`avenue_mcp.errors` -- `errors` is imported by everything, and `config` imports
this, so an errors -> config -> institutions -> errors chain would cycle.
Unknown ids raise ValueError and `config` translates it to ConfigError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping
from urllib.parse import urlparse

# "permitted"/"denied" mean an authenticated probe measured it on THIS instance.
# "unverified" means nobody has looked. It is the default for a reason.
Capability = Literal["permitted", "denied", "unverified"]

# The closed set of routes scripts/probe.py reports on.
CAPABILITY_KEYS: tuple[str, ...] = (
    "course_details",
    "dropbox_folders",
    "dropbox_mysubmissions",
    "grade_objects",
    "grade_weights",
    "calendar_myevents",
    "quizzes",
    "quiz_attempts",
    "classlist",
    "orgunit_users",
)


@dataclass(frozen=True)
class Institution:
    """One school's Brightspace deployment."""

    id: str
    org_name: str
    lms_name: str
    base_url: str
    login_url: str
    credential_brand: str
    timezone: str = "America/Toronto"
    capabilities: Mapping[str, Capability] = field(default_factory=dict)
    probed_at: str | None = None
    probe_doc: str | None = None

    def capability(self, key: str) -> Capability:
        """What a probe measured for `key`, or "unverified" if none has run."""
        return self.capabilities.get(key, "unverified")

    @property
    def verified(self) -> bool:
        """Has an authenticated probe been run against this instance?"""
        return self.probed_at is not None

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc

    @property
    def display(self) -> str:
        """e.g. "Avenue to Learn (McMaster University)"."""
        return f"{self.lms_name} ({self.org_name})"


MCMASTER = Institution(
    id="mcmaster",
    org_name="McMaster University",
    lms_name="Avenue to Learn",
    # avenue.mcmaster.ca is a static Apache landing page, NOT the Brightspace
    # application -- every /d2l/* path 404s there. Login STARTS on the landing
    # page (login.php -> Microsoft Entra SAML) and lands on the Brightspace
    # host, so the two are configured separately.
    base_url="https://avenue.cllmcmaster.ca",
    login_url="https://avenue.mcmaster.ca/login.php",
    credential_brand="MacID",
    capabilities={
        "course_details": "denied",
        "dropbox_folders": "permitted",
        # Documented as a Learner route, and blocked anyway. Submission status
        # is simply not knowable here.
        "dropbox_mysubmissions": "denied",
        "grade_objects": "permitted",
        "grade_weights": "permitted",
        "calendar_myevents": "permitted",
        "quizzes": "permitted",
        "quiz_attempts": "denied",
        # Predicted 403 in docs/02, measured readable. get_class_list withholds
        # the roster on FIPPA grounds regardless -- see tools/classlist.py.
        "classlist": "permitted",
        "orgunit_users": "denied",
    },
    probed_at="2026-08-07",
    probe_doc="docs/08-api-probe-results.md",
)

CARLETON = Institution(
    id="carleton",
    org_name="Carleton University",
    # Carleton does not brand its instance. It is "Brightspace".
    lms_name="Brightspace",
    base_url="https://brightspace.carleton.ca",
    # MUST be the explicit SAML initiator. https://brightspace.carleton.ca/ and
    # /d2l/login both serve a LOCAL D2L username/password box that MyCarletonOne
    # credentials do not work in; /d2l/lp/auth/saml/init 404s. Only this path
    # 302s to the ADFS IdP at cufed.carleton.ca.
    login_url="https://brightspace.carleton.ca/d2l/lp/auth/saml/login",
    credential_brand="MyCarletonOne",
    # Measured 2026-08-07 against a real Carleton student account, on a course
    # carrying assignments, grades, quizzes, and forums. See docs/09.
    capabilities={
        "course_details": "denied",
        "dropbox_folders": "permitted",
        # Denied, same as McMaster -- but it took a per-folder check to see it.
        # scripts/probe.py samples folders[0], which on the probed course held a
        # leftover scratch folder that returns 200 + []. Every real assignment
        # folder returns 403. Reading "permitted" off that one folder is the
        # false positive docs/08 warns about, arriving from the other direction:
        # not an empty course looking blocked, but an anomalous folder looking
        # permitted. Verify this route across folders, never on a sample of one.
        "dropbox_mysubmissions": "denied",
        "grade_objects": "permitted",
        "grade_weights": "permitted",
        # Readable, but returned zero events on the probed course -- so it is
        # permitted yet not a dependable deadline source here. Same as McMaster.
        "calendar_myevents": "permitted",
        "quizzes": "permitted",
        "quiz_attempts": "denied",
        # Returns a full roster including Email and OrgDefinedId -- more than
        # McMaster exposes. get_class_list withholds it on FIPPA grounds anyway.
        "classlist": "permitted",
        "orgunit_users": "denied",
    },
    probed_at="2026-08-07",
    probe_doc="docs/09-carleton-probe-results.md",
)

INSTITUTIONS: dict[str, Institution] = {
    MCMASTER.id: MCMASTER,
    CARLETON.id: CARLETON,
}

DEFAULT_INSTITUTION = MCMASTER.id


def get_institution(key: str) -> Institution:
    """Look up a profile by id. Raises ValueError naming the valid ids."""
    try:
        return INSTITUTIONS[key.strip().lower()]
    except (KeyError, AttributeError):
        valid = ", ".join(sorted(INSTITUTIONS))
        raise ValueError(
            f"Unknown institution {key!r}. Valid ids: {valid}. "
            f"For a school that is not listed, set AVENUE_MCP_BASE_URL and "
            f"AVENUE_MCP_LOGIN_URL directly instead."
        ) from None


def custom_institution(base_url: str, timezone: str = "America/Toronto") -> Institution:
    """A profile for a school with no registry entry.

    Used when base_url is overridden to something the selected profile does not
    describe. Everything is deliberately unbranded and unverified: inheriting
    another school's name -- or worse, its measured capabilities -- is exactly
    the lie the capability system exists to prevent.
    """
    host = urlparse(base_url).netloc or base_url
    return Institution(
        id=host.replace(".", "-").lower() or "custom",
        org_name=host,
        lms_name="Brightspace",
        base_url=base_url.rstrip("/"),
        login_url=base_url.rstrip("/"),
        credential_brand="your university account",
        timezone=timezone,
        capabilities={},
        probed_at=None,
    )
