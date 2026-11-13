import pytest

from pydb import errors


@pytest.mark.parametrize(
    "error_type",
    [
        errors.NotSupportedError,
        errors.StorageError,
        errors.CorruptPageError,
        errors.RowTooLargeError,
    ],
)
def test_every_error_is_a_pydb_error(error_type: type[errors.PyDBError]) -> None:
    with pytest.raises(errors.PyDBError, match="boom"):
        raise error_type("boom")


def test_storage_errors_share_a_base() -> None:
    assert issubclass(errors.CorruptPageError, errors.StorageError)
    assert issubclass(errors.RowTooLargeError, errors.StorageError)
