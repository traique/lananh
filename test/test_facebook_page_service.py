import pytest

from services import facebook_page_service


def test_settings_require_page_credentials(monkeypatch):
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)
    with pytest.raises(facebook_page_service.FacebookPublishError):
        facebook_page_service._settings()
