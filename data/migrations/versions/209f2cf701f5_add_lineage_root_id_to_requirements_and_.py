"""add lineage root_id to requirements and diagrams

Revision ID: 209f2cf701f5
Revises: ad7eed11dc6e
Create Date: 2026-08-18 15:44:22.639909

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '209f2cf701f5'
down_revision: Union[str, Sequence[str], None] = 'ad7eed11dc6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Added nullable first, backfilled (root_id = own id, i.e. every pre-existing row
    # becomes the root of its own lineage), then tightened to NOT NULL — safe even if
    # rows already exist.
    op.add_column('diagrams', sa.Column('root_id', sa.UUID(), nullable=True))
    op.execute('UPDATE diagrams SET root_id = id WHERE root_id IS NULL')
    op.alter_column('diagrams', 'root_id', nullable=False)
    op.create_index(op.f('ix_diagrams_root_id'), 'diagrams', ['root_id'], unique=False)
    op.create_foreign_key(
        'fk_diagrams_root_id_diagrams', 'diagrams', 'diagrams', ['root_id'], ['id'], ondelete='SET NULL'
    )

    op.add_column('requirements', sa.Column('root_id', sa.UUID(), nullable=True))
    op.execute('UPDATE requirements SET root_id = id WHERE root_id IS NULL')
    op.alter_column('requirements', 'root_id', nullable=False)
    op.create_index(op.f('ix_requirements_root_id'), 'requirements', ['root_id'], unique=False)
    op.create_foreign_key(
        'fk_requirements_root_id_requirements', 'requirements', 'requirements', ['root_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_requirements_root_id_requirements', 'requirements', type_='foreignkey')
    op.drop_index(op.f('ix_requirements_root_id'), table_name='requirements')
    op.drop_column('requirements', 'root_id')

    op.drop_constraint('fk_diagrams_root_id_diagrams', 'diagrams', type_='foreignkey')
    op.drop_index(op.f('ix_diagrams_root_id'), table_name='diagrams')
    op.drop_column('diagrams', 'root_id')
