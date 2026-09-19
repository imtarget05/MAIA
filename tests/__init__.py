"""Test package marker.

Required so sibling test modules with identical basenames can coexist
(``tests/test_authorization.py`` vs ``tests/servicedesk/test_authorization.py``)
and so ``from tests.servicedesk.conftest import ...`` resolves.
"""