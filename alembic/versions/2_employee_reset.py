"""employee identity + password reset tokens

Revision ID: 2_employee_reset
Revises: 1_add_auth_tables
Create Date: 2026-09-07 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '2_employee_reset'
down_revision = '1_add_auth_tables'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('employee_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('full_name', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('department', sa.String(length=255), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_employee_id'), ['employee_id'], unique=True)

    op.create_table(
        'password_reset_tokens',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_password_reset_tokens_token_hash'),
                    'password_reset_tokens', ['token_hash'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_password_reset_tokens_token_hash'),
                  table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_employee_id'))
        batch_op.drop_column('department')
        batch_op.drop_column('full_name')
        batch_op.drop_column('employee_id')
