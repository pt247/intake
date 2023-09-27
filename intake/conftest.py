# -----------------------------------------------------------------------------
# Copyright (c) 2012 - 2018, Anaconda, Inc. and Intake contributors
# All rights reserved.
#
# The full license is in the LICENSE file, distributed with this software.
# -----------------------------------------------------------------------------

import os
import posixpath
import tempfile

import pytest
import requests

from intake import config, open_catalog, register_driver
from intake.source.base import DataSource, Schema
from intake.tests.test_utils import copy_test_file

here = os.path.dirname(__file__)


MIN_PORT = 7480
MAX_PORT = 7489
PORT = MIN_PORT

# ensures "object" dtype on strings in dask, which is still the default for pandas
import dask

dask.config.set({"dataframe.convert-string": False})


class TestSource(DataSource):
    name = "test"
    container = "python"

    def __init__(self, **kwargs):
        self.test_kwargs = kwargs
        super().__init__()

    def _get_schema(self):
        return Schema()


register_driver("test", TestSource)


@pytest.fixture
def tmp_config_path(tmp_path):
    key = "INTAKE_CONF_FILE"
    original = os.getenv(key)
    temp_config_path = make_path_posix(os.path.join(tmp_path, "test_config.yml"))
    os.environ[key] = temp_config_path
    assert config.cfile() == temp_config_path
    yield temp_config_path
    config.conf.reset()
    if original:
        os.environ[key] = original
    else:
        del os.environ[key]
    assert config.cfile() != temp_config_path


def ping_server(url, swallow_exception, head=None):
    try:
        r = requests.get(url)
    except Exception as e:
        if swallow_exception:
            return False
        else:
            raise e

    return r.status_code in (200, 403)  # allow forbidden as well


def pick_port():
    global PORT
    port = PORT
    if port == MAX_PORT:
        PORT = MIN_PORT
    else:
        PORT += 1

    return port


@pytest.fixture(scope="function")
def env(temp_cache, tempdir):
    import intake

    env = os.environ.copy()
    env["INTAKE_CONF_DIR"] = intake.config.confdir
    env["INTAKE_CACHE_DIR"] = intake.config.conf["cache_dir"]
    return env


@pytest.fixture
def inherit_params_cat():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = posixpath.join(tmp_dir, "intake")
        target_catalog = copy_test_file("catalog_inherit_params.yml", tmp_path)
        return open_catalog(target_catalog)


@pytest.fixture
def inherit_params_multiple_cats():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = posixpath.join(tmp_dir, "intake")
        copy_test_file("catalog_inherit_params.yml", tmp_path)
        copy_test_file("catalog_nested_sub.yml", tmp_path)
        return open_catalog(tmp_path + "/*.yml")


@pytest.fixture
def inherit_params_subcat():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = posixpath.join(tmp_dir, "intake")
        target_catalog = copy_test_file("catalog_inherit_params.yml", tmp_path)
        copy_test_file("catalog_nested_sub.yml", tmp_path)
        return open_catalog(target_catalog)
