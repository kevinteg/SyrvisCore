"""Drift guards for the seam registry's copied constants.

STACK_SERVICES/STACK_PRIMORDIAL are duplicated in syrviscore.seam.registry so
the generator stays stdlib-only when run straight from the source tree; these
tests are the binding that keeps the copy honest.
"""

from syrviscore import stack
from syrviscore.seam import registry


def test_stack_services_match_platform():
    assert registry.STACK_SERVICES == stack.ALL_SERVICES


def test_stack_primordial_match_platform():
    assert registry.STACK_PRIMORDIAL == stack.PRIMORDIAL
