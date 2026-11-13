import pytest
from hypothesis import given
from hypothesis import strategies as st

from pydb.config import PAGE_SIZE
from pydb.errors import CorruptPageError
from pydb.storage import page
from pydb.storage.page import PageHeader, PageType


def test_header_is_24_bytes() -> None:
    assert page.HEADER_SIZE == 24


def test_new_page_has_header_and_zero_body() -> None:
    buf = page.new_page(7, PageType.EMPTY)
    assert len(buf) == PAGE_SIZE
    assert page.read_header(buf) == PageHeader(7, PageType.EMPTY, 0)
    assert buf[page.HEADER_SIZE :] == bytes(PAGE_SIZE - page.HEADER_SIZE)


def test_header_round_trip_with_lsn() -> None:
    buf = page.new_page(3, PageType.EMPTY)
    page.write_header(buf, PageHeader(3, PageType.FREE, 2**40 + 5))
    assert page.read_header(buf) == PageHeader(3, PageType.FREE, 2**40 + 5)


def test_header_byte_layout() -> None:
    buf = page.new_page(0x01020304, PageType.HEADER)
    page.write_header(buf, PageHeader(0x01020304, PageType.HEADER, 9))
    page.seal(buf)
    assert buf[0:4] == bytes([4, 3, 2, 1])  # page_id, little-endian
    assert buf[4] == PageType.HEADER
    assert buf[8:16] == (9).to_bytes(8, "little")
    assert buf[16:20] == page.compute_checksum(buf).to_bytes(4, "little")


def test_sealed_page_verifies() -> None:
    buf = page.new_page(5, PageType.EMPTY)
    buf[100:105] = b"hello"
    page.seal(buf)
    assert page.verify(buf, 5).page_id == 5


def test_checksum_ignores_its_own_field() -> None:
    buf = page.new_page(1, PageType.EMPTY)
    before = page.compute_checksum(buf)
    buf[page.CHECKSUM_OFFSET : page.CHECKSUM_END] = b"\xff\xff\xff\xff"
    assert page.compute_checksum(buf) == before


@given(
    offset=st.integers(0, PAGE_SIZE - 1).filter(
        lambda o: not page.CHECKSUM_OFFSET <= o < page.CHECKSUM_END
    ),
    bit=st.integers(0, 7),
)
def test_any_single_bit_flip_is_detected(offset: int, bit: int) -> None:
    buf = page.new_page(2, PageType.EMPTY)
    buf[200:210] = b"0123456789"
    page.seal(buf)
    buf[offset] ^= 1 << bit
    with pytest.raises(CorruptPageError):
        page.verify(buf, 2)


def test_corrupted_checksum_field_is_detected() -> None:
    buf = page.new_page(2, PageType.EMPTY)
    page.seal(buf)
    buf[page.CHECKSUM_OFFSET] ^= 0x01
    with pytest.raises(CorruptPageError, match="checksum"):
        page.verify(buf, 2)


def test_misdirected_page_is_detected() -> None:
    buf = page.new_page(4, PageType.EMPTY)
    page.seal(buf)
    with pytest.raises(CorruptPageError, match="misdirected"):
        page.verify(buf, 9)


def test_wrong_size_is_detected() -> None:
    with pytest.raises(CorruptPageError, match="bytes"):
        page.verify(bytes(100), 1)


def test_unknown_page_type_is_corrupt() -> None:
    buf = page.new_page(1, PageType.EMPTY)
    buf[4] = 250
    page.seal(buf)
    with pytest.raises(CorruptPageError, match="unknown page type"):
        page.verify(buf, 1)
