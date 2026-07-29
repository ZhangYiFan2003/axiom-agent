import os

import python_pkg.utils as utils_alias

from .utils import Helper
from .utils import load_config as load_settings


class Service:
    def __init__(self):
        self.helper = Helper()

    def validate(self, value: str) -> bool:
        return self.helper.validate(value)

    def run(self) -> dict[str, str]:
        os.getcwd()
        load_settings("app.toml")
        utils_alias.load_config("app.toml")
        method_name = "validate"
        getattr(self.helper, method_name)(value="x")
        return load_settings("app.toml")
