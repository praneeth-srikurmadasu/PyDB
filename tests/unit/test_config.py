from pydb import config


def test_page_size_is_a_power_of_two() -> None:
    assert config.PAGE_SIZE > 0
    assert config.PAGE_SIZE & (config.PAGE_SIZE - 1) == 0


def test_page_ids() -> None:
    assert config.HEADER_PAGE_ID == 0
    assert config.INVALID_PAGE_ID < 0


def test_buffer_pool_default() -> None:
    assert config.BUFFER_POOL_SIZE >= 1


def test_text_limits_fit_in_a_page() -> None:
    assert 0 < config.MAX_KEY_TEXT_BYTES <= config.MAX_TEXT_BYTES < config.PAGE_SIZE
