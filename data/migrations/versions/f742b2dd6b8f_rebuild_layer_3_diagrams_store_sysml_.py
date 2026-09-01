"""rebuild layer 3: diagrams store sysml_text and derived mermaid

Revision ID: f742b2dd6b8f
Revises: 6132c1ad5321
Create Date: 2026-08-30 13:20:58.373995

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f742b2dd6b8f'
down_revision: Union[str, Sequence[str], None] = '6132c1ad5321'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    NOTE: autogenerate also proposed dropping the checkpoint_* tables — those belong to
    langgraph's AsyncPostgresSaver (created via its own .setup(), not our models) and
    must NOT be touched by our migrations. Only the `diagrams` column changes are kept.
    """
    op.add_column('diagrams', sa.Column('sysml_text', sa.Text(), nullable=True))
    op.execute('UPDATE diagrams SET sysml_text = plantuml WHERE sysml_text IS NULL')
    op.alter_column('diagrams', 'sysml_text', nullable=False)
    op.add_column('diagrams', sa.Column('mermaid', sa.Text(), nullable=True))
    op.drop_column('diagrams', 'plantuml')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('diagrams', sa.Column('plantuml', sa.TEXT(), nullable=True))
    op.execute('UPDATE diagrams SET plantuml = sysml_text WHERE plantuml IS NULL')
    op.alter_column('diagrams', 'plantuml', nullable=False)
    op.drop_column('diagrams', 'mermaid')
    op.drop_column('diagrams', 'sysml_text')
