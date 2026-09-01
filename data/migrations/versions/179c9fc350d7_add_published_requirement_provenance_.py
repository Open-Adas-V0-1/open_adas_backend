"""add published requirement provenance link to requirements

Revision ID: 179c9fc350d7
Revises: 209f2cf701f5
Create Date: 2026-08-18 17:19:55.540534

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '179c9fc350d7'
down_revision: Union[str, Sequence[str], None] = '209f2cf701f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('requirements', sa.Column('source_published_requirement_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_requirements_source_published_requirement_id', 'requirements', 'published_requirements',
        ['source_published_requirement_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_requirements_source_published_requirement_id', 'requirements', type_='foreignkey')
    op.drop_column('requirements', 'source_published_requirement_id')
