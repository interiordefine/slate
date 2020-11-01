"""empty message

Revision ID: 87bffdc53429
Revises: 
Create Date: 2020-11-01 13:23:28.461325

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '87bffdc53429'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('auth', sa.Column('created_at', sa.DateTime(), nullable=False))
    op.add_column('auth', sa.Column('is_active', sa.Boolean(), nullable=True))
    op.add_column('standup', sa.Column('created_at', sa.DateTime(), nullable=False))
    op.add_column('user', sa.Column('created_at', sa.DateTime(), nullable=False))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('user', 'created_at')
    op.drop_column('standup', 'created_at')
    op.drop_column('auth', 'is_active')
    op.drop_column('auth', 'created_at')
    # ### end Alembic commands ###