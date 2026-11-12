import pytest

from setuppilot.config import GitHubApp, load_settings
from setuppilot.errors import ConfigInvalid
from tests.test_config import REAL_CONFIG, write_config


def test_sign_in_is_off_until_you_register_your_own_app():
    assert load_settings(REAL_CONFIG).github.app is None


def test_the_app_id_given_as_the_client_id_is_refused(tmp_path):
    app = {"client_id": "1234567", "app_slug": "setuppilot-me"}
    path = write_config(tmp_path, lambda d: d["github"].update(app=app))
    with pytest.raises(ConfigInvalid, match="looks like the App ID"):
        load_settings(path)


def test_the_install_link_comes_from_the_app_slug():
    app = GitHubApp(client_id="Iv23liExample", app_slug="setuppilot-me")
    assert app.install_url == "https://github.com/apps/setuppilot-me/installations/new"
