import pytest

from artemis.utils.device_kind import device_title


@pytest.mark.parametrize(
    ("model", "product", "expected"),
    [
        (" Pixel 6 Pro ", "raven", "Pixel 6 Pro"),
        (None, " raven ", "raven"),
        ("  ", "raven", "raven"),
        (None, None, "Android phone"),
        ("", "  ", "Android phone"),
    ],
)
def test_device_title_uses_model_then_product_then_android_phone(model, product, expected):
    assert device_title(model, product) == expected
