"""Service Desk initial schema (S4) — all ``sd_`` tables.

revision id: 0001_service_desk
down_revision: None

The initial revision creates every table from the live SQLAlchemy metadata
(``Base.metadata.create_all``), which guarantees the migration and the
models cannot drift: constraints (UNIQUE keys, CHECK state machines, FKs)
are defined once in ``models.py``. Later revisions must use explicit
op.* operations per the usual Alembic workflow.

Failure convention: applying this migration to a database that already
contains Service Desk tables is a no-op error-free run only when the
schema matches; conflicting objects raise and abort the upgrade.
"""
from alembic import op

revision = "0001_service_desk"
down_revision = None
branch_labels = ("service_desk",)
depends_on = None


def upgrade() -> None:
    from maia.servicedesk.models import Base

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from maia.servicedesk.models import Base

    Base.metadata.drop_all(bind=op.get_bind())
