"""MySQL lock failures must escape helpers that share a caller's transaction."""


def is_retryable_lock_error(exc):
    return bool(getattr(exc, "args", ())) and exc.args[0] in (1205, 1213)


def propagate_transaction_abort(exc):
    # InnoDB can roll back the entire transaction on a deadlock. Treating a
    # failed notification/history insert as best effort could then report an
    # order as saved even though its earlier writes were rolled back.
    if is_retryable_lock_error(exc):
        raise exc
