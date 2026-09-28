from __future__ import annotations

import pytest

from stationapp.concurrency.cancellation import ( CancellationToken, OperationCancelled )

@pytest.mark.unit
def test_new_token_is_not_cancelled() -> None:
    token = CancellationToken()
    assert token.is_cancelled is False
    
@pytest.mark.unit
def test_cancel_marks_token() -> None:
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled is True
    
@pytest.mark.unit
def test_raise_if_cancelled_raises() -> None:
    token = CancellationToken()
    token.cancel()
    
    with pytest.raises(OperationCancelled):
        token.raise_if_cancelled()