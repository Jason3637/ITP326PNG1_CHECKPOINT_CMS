"""applicant residence, employer and ID document type (M5)

Revision ID: a3f1c9e7d2b4
Revises: 5e8d2c1f9a47
Create Date: 2026-10-03 10:00:00.000000

The Loan Officer review screen showed residence, employer and ID type as
"not collected":
  * loan_applications.residential_address / employer_name - genuinely never
    captured before; now collected at apply time. Existing applications stay
    NULL (nothing to backfill from - an officer can ask via Request More
    Information).
  * documents.id_document_type - the apply form always asked for the ID type
    but could only smuggle it into the uploaded filename, which ends up in
    storage_path as "..._<id type>-<name>". Backfilled from there.

(The fourth field, the interest rate, needed no schema change: it was
always computed by prime_pricing.calculate_prime(), just not serialized.)
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a3f1c9e7d2b4'
down_revision = '5e8d2c1f9a47'
branch_labels = None
depends_on = None

_ID_TYPES = ('national_id', 'drivers_licence', 'passport', 'work_id')


def upgrade():
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('residential_address', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('employer_name', sa.String(length=255), nullable=True))

    # add_column() doesn't create a new Postgres enum type by itself.
    id_type = sa.Enum(*_ID_TYPES, name='id_document_type')
    id_type.create(op.get_bind(), checkfirst=True)
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('id_document_type', id_type, nullable=True))

    if op.get_bind().dialect.name == 'postgresql':
        # storage_path = users/<id>/id_verification/<hex12>_<id type>-<name>.<ext>
        op.execute(
            f"""
            UPDATE documents
            SET id_document_type = (substring(storage_path from
                    '_({'|'.join(_ID_TYPES)})-'))::id_document_type
            WHERE document_type = 'id_verification'
              AND id_document_type IS NULL
              AND storage_path ~ '_({'|'.join(_ID_TYPES)})-'
            """
        )


def downgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('id_document_type')
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.drop_column('employer_name')
        batch_op.drop_column('residential_address')
    if op.get_bind().dialect.name == 'postgresql':
        sa.Enum(name='id_document_type').drop(op.get_bind(), checkfirst=True)
