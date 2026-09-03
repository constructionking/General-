"""Pure helpers of the Playwright driver: proxy passthrough, dialog classification, exception hierarchy."""
from bhulekh.browser import (PortalDialog, PortalError, PortalServerError, _env_proxy, dialog_means_no_records,
                             parse_row, row_from_api)


def test_env_proxy_absent():
    assert _env_proxy({}) is None


def test_env_proxy_plain_and_bypass():
    p = _env_proxy({"HTTPS_PROXY": "http://127.0.0.1:44207", "NO_PROXY": "localhost,10.0.0.0/8"})
    assert p == {"server": "http://127.0.0.1:44207", "bypass": "localhost,10.0.0.0/8"}


def test_env_proxy_credentials_are_split_out():
    p = _env_proxy({"https_proxy": "http://alice:s%23cret@proxy.example:3128"})
    assert p == {"server": "http://proxy.example:3128", "username": "alice", "password": "s#cret"}


def test_dialog_classification():
    assert dialog_means_no_records("×iinfo!यह गाँव चकबंदी में है।OKNoCancel")
    assert dialog_means_no_records("No Data Found")
    assert dialog_means_no_records("कोई अभिलेख उपलब्ध नहीं")
    assert not dialog_means_no_records("Something went wrong, please try again")
    assert not dialog_means_no_records("Session expired")


def test_exception_hierarchy():
    assert issubclass(PortalDialog, PortalError)
    assert issubclass(PortalServerError, PortalError)
    assert not issubclass(PortalServerError, PortalDialog)


def test_row_from_api_matches_rendered_parse():
    api = row_from_api({"khasra_no": "71", "name": "संजय सिंह", "father": "रामसरन सिंह",
                        "unique_code": "1179440071000012", "area": "0.1740"})
    rendered = parse_row("71 : संजय सिंह : रामसरन सिंह : 1179440071000012 : (0.1740 हे०)")
    assert (api.khata, api.khatedar, api.father, api.unique_code, api.area) == \
           (rendered.khata, rendered.khatedar, rendered.father, rendered.unique_code, rendered.area)
