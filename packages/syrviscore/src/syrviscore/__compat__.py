"""Manager/service compatibility contract.

The manager (`syrvisctl`) and service (`syrvis`) version-skew freely by design —
a 0.2.x manager has driven 0.3.x services in production. This module is how a
service release declares when that stops being safe: bump MIN_MANAGER_VERSION
only when the service starts depending on a newer manager behavior (a manifest
schema change, a new install-layout expectation, ...).

`syrvisctl activate` probes this constant via the version's own venv python and
refuses activation with a typed CompatibilityError when its own version is
older. A missing module (older service) or failed probe means "no declared
constraint" — fully backward compatible.
"""

MIN_MANAGER_VERSION = "0.2.0"
