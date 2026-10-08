import app


def test_app_package_imports() -> None:
    assert app.__name__ == "app"
