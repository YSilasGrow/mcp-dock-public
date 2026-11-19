"""Start the local bridge workbench if needed, then open it in a browser."""

import os
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT = Path(__file__).resolve().parent
PYTHON = PROJECT / ".venv/bin/python"
SERVER = PROJECT / "run_workbench.py"
URL = "http://127.0.0.1:57420/"
TITLE = "小龙女 × 大肥鱼 · 协作工作台"


def workbench_ready() -> bool:
    try:
        with urlopen(URL, timeout=0.5) as response:
            return TITLE in response.read(65536).decode("utf-8", errors="replace")
    except (OSError, URLError):
        return False


def main() -> None:
    if not workbench_ready():
        if not PYTHON.is_file() or not SERVER.is_file():
            raise RuntimeError("桥接项目或 Python 环境不存在。")
        log_dir = PROJECT / ".local/workbench"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_fd = os.open(log_dir / "launcher.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(log_fd, "ab") as log:
            process = subprocess.Popen(
                [str(PYTHON), str(SERVER)],
                cwd=PROJECT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if workbench_ready():
                break
            if process.poll() is not None:
                raise RuntimeError("工作台启动失败，请查看项目内 .local/workbench/launcher.log。")
            time.sleep(0.2)
        else:
            raise RuntimeError("工作台在 8 秒内未启动，请查看项目内 .local/workbench/launcher.log。")
    subprocess.run(["/usr/bin/open", URL], check=True)


if __name__ == "__main__":
    main()
