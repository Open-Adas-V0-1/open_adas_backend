"""add gen_id to requirements and diagrams

Revision ID: a0da045e4845
Revises: 7877d8b2726c
Create Date: 2026-09-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a0da045e4845'
down_revision: Union[str, Sequence[str], None] = '7877d8b2726c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('requirements', sa.Column('gen_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_requirements_gen_id'), 'requirements', ['gen_id'], unique=False)
    op.add_column('diagrams', sa.Column('gen_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_diagrams_gen_id'), 'diagrams', ['gen_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_diagrams_gen_id'), table_name='diagrams')
    op.drop_column('diagrams', 'gen_id')
    op.drop_index(op.f('ix_requirements_gen_id'), table_name='requirements')
    op.drop_column('requirements', 'gen_id')
