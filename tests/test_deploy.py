import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
INSTALL_SH = DEPLOY_DIR / "install.sh"
BACKUP_SH = DEPLOY_DIR / "backup.sh"
SERVICE_UNIT = DEPLOY_DIR / "galochka.service"
GALOCHKA_CLI = DEPLOY_DIR / "galochka-cli"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def test_scripts_have_valid_bash_syntax():
    for script in (INSTALL_SH, BACKUP_SH):
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_systemd_unit_has_required_directives():
    text = SERVICE_UNIT.read_text()
    assert "ExecStart=/opt/galochka/.venv/bin/galochka serve" in text
    assert "EnvironmentFile=/etc/galochka.env" in text


def test_galochka_cli_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(GALOCHKA_CLI)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_copies_galochka_cli_to_usr_local_bin():
    text = INSTALL_SH.read_text()
    assert "install -m 0755" in text
    assert "/usr/local/bin/galochka-cli" in text


def test_password_generation_survives_pipefail():
    # Regression test: under `set -euo pipefail`, a `tr | head -cN` pipeline
    # reading directly from /dev/urandom fails (tr is killed by SIGPIPE when
    # head closes the pipe early, exit 141), which would abort install.sh
    # before it ever writes /etc/galochka.env. Extract the actual
    # PASSWORD=... line from install.sh and run it under the same shell
    # options to make sure it does not have that problem and produces a
    # 12-character password.
    lines = [
        line.strip()
        for line in INSTALL_SH.read_text().splitlines()
        if line.strip().startswith("PASSWORD=")
    ]
    assert len(lines) == 1, lines
    password_line = lines[0]

    script = f'set -euo pipefail; {password_line}; [ "${{#PASSWORD}}" -eq 12 ]; echo ok'
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def _make_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    subprocess.run(
        ["sqlite3", str(data_dir / "galochka.db"), "CREATE TABLE t (id INTEGER);"],
        check=True,
    )
    photos_dir = data_dir / "photos"
    photos_dir.mkdir()
    (photos_dir / "A01143_front.jpg").write_bytes(b"fake-photo-bytes")


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 not available")
def test_backup_creates_archive_with_db_and_photos(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _make_data_dir(data_dir)

    env = dict(os.environ)
    env["GALOCHKA_DATA"] = str(data_dir)
    result = subprocess.run(
        ["bash", str(BACKUP_SH), str(out_dir)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr

    archive_path = Path(result.stdout.strip())
    assert archive_path.exists()
    assert archive_path.parent == out_dir

    listing = subprocess.run(
        ["tar", "-tzf", str(archive_path)], capture_output=True, text=True, check=True
    ).stdout
    assert "galochka.db" in listing
    assert "photos/A01143_front.jpg" in listing


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 not available")
def test_backup_keeps_only_newest_8_archives(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _make_data_dir(data_dir)
    out_dir.mkdir()

    for i in range(1, 10):
        (out_dir / f"galochka-2020-01-{i:02d}.tar.gz").write_bytes(b"dummy")
    assert len(list(out_dir.glob("galochka-*.tar.gz"))) == 9

    env = dict(os.environ)
    env["GALOCHKA_DATA"] = str(data_dir)
    result = subprocess.run(
        ["bash", str(BACKUP_SH), str(out_dir)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr

    remaining = sorted(p.name for p in out_dir.glob("galochka-*.tar.gz"))
    assert len(remaining) == 8
    # the oldest dummy archive (2020-01-01) must have been pruned
    assert "galochka-2020-01-01.tar.gz" not in remaining


def test_install_sh_writes_galochka_env_file():
    text = INSTALL_SH.read_text()
    for line in (
        "GALOCHKA_DATA=$DATA_DIR",
        "GALOCHKA_PORT=8081",
        "GALOCHKA_EXPERT_PASSWORD=$PASSWORD",
        "GALOCHKA_CASSANDRA_URL=http://cassandra",
    ):
        assert line in text, line
    assert "pip\" install -e ." in text
    # no ML leftovers from Cassandra
    for banned in ("torch", "transformers", "HF_HOME", "HF_HUB_OFFLINE", "cassandra.service"):
        assert banned not in text.replace("http://cassandra", ""), banned
    assert "http://$IP:8081" in text
    assert "Generated expert password" in text


def test_cli_wrapper_preserves_galochka_env():
    text = GALOCHKA_CLI.read_text()
    assert "/etc/galochka.env" in text
    assert "GALOCHKA_DATA" in text and "GALOCHKA_EXPERT_PASSWORD" in text
    assert "sudo -u galochka" in text


def test_data_dir_and_files_are_not_world_readable():
    assert 'chmod 0750 "$DATA_DIR"' in INSTALL_SH.read_text()
    assert "UMask=0027" in SERVICE_UNIT.read_text()
    lines = [line.strip() for line in BACKUP_SH.read_text().splitlines()]
    assert "umask 077" in lines
    assert lines.index("umask 077") < lines.index('mkdir -p "$TARGET"')


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 not available")
def test_backup_archive_is_private(tmp_path):
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    _make_data_dir(data_dir)
    env = dict(os.environ, GALOCHKA_DATA=str(data_dir))
    result = subprocess.run(["bash", str(BACKUP_SH), str(out_dir)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).stat().st_mode & 0o077 == 0
