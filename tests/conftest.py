"""
テスト共通設定。
ローカル正典ファイル（`_xxx_remote.py`）を、VPS配置時の名前（`xxx`）で
import できるように動的に読み込む。
"""
import os
import sys
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _load_local_module(remote_filename: str, module_name: str) -> None:
    """`_zoom_xxx_remote.py` を `zoom_xxx` として sys.modules に登録"""
    path = os.path.join(_ROOT, remote_filename)
    if not os.path.exists(path):
        return
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[module_name] = mod


_load_local_module("_zoom_webhook_remote.py", "zoom_webhook")
_load_local_module("_zoom_client_remote.py", "zoom_client")
