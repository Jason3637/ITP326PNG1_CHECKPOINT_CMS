"""prime workflow revision - status enum value swap

Revision ID: c744ece0db7c
Revises: 627a6b23d180
Create Date: 2026-09-30 00:50:11.220029

The risky migration, isolated from 627a6b23d180's purely-additive changes so
it can be reviewed/run/rolled-back on its own. Native Postgres ENUM types
can't have values renamed or removed in place while a column uses them, so
each of the three enums below is swapped via: rename the old type out of the
way, create a new type under the original name with the target value list,
move the column over with an explicit USING...CASE mapping (any value NOT
covered by the CASE falls through to NULL, which then fails the column's
NOT NULL constraint - the migration aborts loudly instead of silently
mis-mapping a row), then drop the old type.

Old -> new mappings (see app/models/enums.py for the full rationale):
    loan_application_status: pending->submitted, under_review->officer_review,
        approved->approved, rejected->rejected
    loan_status: active->active, completed->paid, defaulted->closed
        (defaulted rows also get loans.closure_reason='defaulted' backfilled)
    payment_status: pending->reported, completed->verified, failed->rejected

document_type's new 'proof_of_income' value was handled in 627a6b23d180
(purely additive - no existing value needed remapping, so it didn't need
this heavier pattern).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c744ece0db7c'
down_revision = '627a6b23d180'
branch_labels = None
depends_on = None


def _swap_enum(
    *,
    table: str,
    column: str,
    type_name: str,
    new_values: tuple[str, ...],
    case_map: dict[str, str],
    new_default: str,
) -> None:
    """Rename type_name -> type_name_old, create a new type_name with
    new_values, move `table.column` over via the case_map, restore a
    default, then drop the old type. Postgres only - no-op elsewhere.
    """
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    old_type_name = f"{type_name}_old"
    values_sql = ", ".join(f"'{v}'" for v in new_values)
    when_sql = "\n            ".join(
        f"WHEN '{old}' THEN '{new}'" for old, new in case_map.items()
    )

    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(f"ALTER TYPE {type_name} RENAME TO {old_type_name}")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({values_sql})")
    op.execute(
        f"""
        ALTER TABLE {table}
        ALTER COLUMN {column} TYPE {type_name}
        USING (CASE {column}::text
            {when_sql}
        END)::{type_name}
        """
    )
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{new_default}'::{type_name}"
    )
    op.execute(f"DROP TYPE {old_type_name}")


def _swap_enum_back(
    *,
    table: str,
    column: str,
    type_name: str,
    old_values: tuple[str, ...],
    reverse_case_map: dict[str, str],
    old_default: str,
) -> None:
    """The mirror-image of _swap_enum(), for downgrade()."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return

    new_type_name = f"{type_name}_new"
    values_sql = ", ".join(f"'{v}'" for v in old_values)
    when_sql = "\n            ".join(
        f"WHEN '{cur}' THEN '{old}'" for cur, old in reverse_case_map.items()
    )

    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(f"ALTER TYPE {type_name} RENAME TO {new_type_name}")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({values_sql})")
    op.execute(
        f"""
        ALTER TABLE {table}
        ALTER COLUMN {column} TYPE {type_name}
        USING (CASE {column}::text
            {when_sql}
        END)::{type_name}
        """
    )
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{old_default}'::{type_name}"
    )
    op.execute(f"DROP TYPE {new_type_name}")


_APPLICATION_STATUS_NEW = (
    "draft", "submitted", "officer_review", "customer_action_required",
    "recommended_for_approval", "admin_review", "approved", "rejected",
    "awaiting_disbursement",
)
_APPLICATION_STATUS_MAP = {
    "pending": "submitted",
    "under_review": "officer_review",
    "approved": "approved",
    "rejected": "rejected",
}
_LOAN_STATUS_NEW = ("active", "overdue", "paid", "closed")
_LOAN_STATUS_MAP = {"active": "active", "completed": "paid", "defaulted": "closed"}
_PAYMENT_STATUS_NEW = ("reported", "verification_pending", "verified", "rejected")
_PAYMENT_STATUS_MAP = {"pending": "reported", "completed": "verified", "failed": "rejected"}


def upgrade():
    _swap_enum(
        table="loan_applications", column="status", type_name="loan_application_status",
        new_values=_APPLICATION_STATUS_NEW, case_map=_APPLICATION_STATUS_MAP,
        new_default="submitted",
    )
    _swap_enum(
        table="loans", column="status", type_name="loan_status",
        new_values=_LOAN_STATUS_NEW, case_map=_LOAN_STATUS_MAP,
        new_default="active",
    )
    # Backfill the compensating signal for the old DEFAULTED status folding
    # into CLOSED (see LoanClosureReason's docstring in app/models/enums.py).
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(
            "UPDATE loans SET closure_reason = 'defaulted' "
            "WHERE status = 'closed' AND closure_reason IS NULL"
        )
    _swap_enum(
        table="payment_transactions", column="status", type_name="payment_status",
        new_values=_PAYMENT_STATUS_NEW, case_map=_PAYMENT_STATUS_MAP,
        new_default="reported",
    )


def downgrade():
    _swap_enum_back(
        table="payment_transactions", column="status", type_name="payment_status",
        old_values=("pending", "completed", "failed"),
        reverse_case_map={"reported": "pending", "verification_pending": "pending",
                           "verified": "completed", "rejected": "failed"},
        old_default="pending",
    )
    _swap_enum_back(
        table="loans", column="status", type_name="loan_status",
        old_values=("active", "completed", "defaulted"),
        reverse_case_map={"active": "active", "overdue": "active",
                           "paid": "completed", "closed": "defaulted"},
        old_default="active",
    )
    _swap_enum_back(
        table="loan_applications", column="status", type_name="loan_application_status",
        old_values=("pending", "under_review", "approved", "rejected"),
        reverse_case_map={
            "draft": "pending", "submitted": "pending", "officer_review": "under_review",
            "customer_action_required": "under_review", "recommended_for_approval": "under_review",
            "admin_review": "under_review", "approved": "approved", "rejected": "rejected",
            "awaiting_disbursement": "approved",
        },
        old_default="pending",
    )
