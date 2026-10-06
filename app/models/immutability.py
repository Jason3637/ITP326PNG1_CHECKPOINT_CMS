"""Insert-only tables: the ORM refuses to update or delete their rows.

This is the in-application layer. On Postgres the same tables also carry a
BEFORE UPDATE OR DELETE trigger (migration d8c1f4a2b6e3), which stops any
client - including someone with direct database access - so this module
mainly turns a coding mistake into an immediate error in tests.

The PRIME quote on an application (pricing/penalty version and the quoted
amounts) is insert-once too: it may be set while it is empty, never changed.
"""

from sqlalchemy import event, inspect

from .disbursement import Disbursement
from .loan_application import LoanApplication
from .loan_records import LoanClosure, LoanLedgerEntry, LoanTermsSnapshot, ReapplicationClearance
from .pricing_policy import (
    PenaltyPolicyTier,
    PenaltyPolicyVersion,
    PrimePricingTier,
    PrimePricingVersion,
)

INSERT_ONLY = (
    LoanTermsSnapshot,
    LoanLedgerEntry,
    LoanClosure,
    ReapplicationClearance,
    Disbursement,
    PrimePricingVersion,
    PrimePricingTier,
    PenaltyPolicyVersion,
    PenaltyPolicyTier,
)

QUOTE_FIELDS = (
    "pricing_version_id",
    "penalty_policy_version_id",
    "quoted_interest_rate",
    "quoted_interest_amount",
    "quoted_total_repayable",
)


class ImmutableRecordError(RuntimeError):
    pass


def _refuse(verb):
    def listener(_mapper, _connection, target):
        raise ImmutableRecordError(
            f"{type(target).__name__} rows are insert-only and cannot be {verb}."
        )

    return listener


for _model in INSERT_ONLY:
    event.listen(_model, "before_update", _refuse("updated"))
    event.listen(_model, "before_delete", _refuse("deleted"))


@event.listens_for(LoanApplication, "before_update")
def _quote_is_insert_once(_mapper, _connection, target):
    state = inspect(target)
    for name in QUOTE_FIELDS:
        history = state.attrs[name].history
        if history.has_changes() and history.deleted and history.deleted[0] is not None:
            raise ImmutableRecordError(
                f"LoanApplication.{name} is part of the locked PRIME quote and cannot change."
            )
