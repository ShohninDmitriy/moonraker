# Moonraker/Klipper update configuration
#
# Copyright (C) 2022  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os
import sys
import copy
import pathlib
from ...utils import source_info
from typing import (
    TYPE_CHECKING,
    Dict,
    Optional
)

if TYPE_CHECKING:
    from ...confighelper import ConfigHelper
    from ..database import MoonrakerDatabase

KLIPPER_DEFAULT_PATH = os.path.expanduser("~/klipper")
KLIPPER_DEFAULT_EXEC = os.path.expanduser("~/klippy-env/bin/python")

BASE_CONFIG: Dict[str, Dict[str, str]] = {
    "moonraker": {
        "origin": "https://github.com/ShohninDmitriy/moonraker.git",
        "requirements": "scripts/moonraker-requirements.txt",
        "venv_args": "-p python3",
        "system_dependencies": "scripts/system-dependencies.json",
        "host_repo": "arksine/moonraker",
        "virtualenv": sys.exec_prefix,
        "path": str(source_info.source_path()),
        "managed_services": "moonraker"
    },
    "klipper": {
        "moved_origin": "https://github.com/ShohninDmitriy/klipper.git",
        "origin": "https://github.com/ShohninDmitriy/klipper.git",
        "requirements": "scripts/klippy-requirements.txt",
        "venv_args": "-p python3",
        "install_script": "scripts/install-octopi.sh",
        "host_repo": "arksine/moonraker",
        "managed_services": "klipper"
    }
}

def get_app_type(app_path: Optional[pathlib.Path] = None) -> str:
    # None type will perform checks on Moonraker
    if source_info.is_git_repo(app_path):
        return "git_repo"
    else:
        return "zip"

def get_base_configuration(config: ConfigHelper, channel: str) -> ConfigHelper:
    server = config.get_server()
    base_cfg = copy.deepcopy(BASE_CONFIG)
    base_cfg["moonraker"]["channel"] = channel
    base_cfg["moonraker"]["type"] = get_app_type()
    base_cfg["klipper"]["channel"] = "beta" if channel == "stable" else channel
    db: MoonrakerDatabase = server.lookup_component('database')
    base_cfg["klipper"]["path"] = db.get_item(
        "moonraker", "update_manager.klipper_path", KLIPPER_DEFAULT_PATH
    ).result()
    base_cfg["klipper"]["env"] = db.get_item(
        "moonraker", "update_manager.klipper_exec", KLIPPER_DEFAULT_EXEC
    ).result()
    klipper_path = pathlib.Path(base_cfg["klipper"]["path"])
    base_cfg["klipper"]["type"] = get_app_type(klipper_path)
    return config.read_supplemental_dict(base_cfg)
