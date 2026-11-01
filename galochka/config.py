import logging
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


log = logging.getLogger("galochka")

DEFAULT_EXPERT_PASSWORD = "experte"
DEFAULT_TZ = "Europe/Berlin"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8081
    expert_password: str = DEFAULT_EXPERT_PASSWORD
    # Base URL of the sister app Cassandra (item catalog and reference photos).
    # Empty = not configured; the CSV import is then the only item source.
    cassandra_url: str = ""
    # Daily background sync of the item catalog from Cassandra (needs cassandra_url).
    # Tests switch it off.
    auto_sync: bool = True
    # IANA time zone of the company: day bounds of the reports/export and the export's Zeitpunkt column.
    tz: str = DEFAULT_TZ

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.environ.get("GALOCHKA_EXPERT_PASSWORD")
        if password is None:
            log.warning("GALOCHKA_EXPERT_PASSWORD is not set: using the default password; set it in /etc/galochka.env")
            password = DEFAULT_EXPERT_PASSWORD
        elif not password.strip():
            raise SystemExit("GALOCHKA_EXPERT_PASSWORD is set but empty: refusing to start. Set a real password.")
        return cls(
            data_dir=Path(os.environ.get("GALOCHKA_DATA", "./data")),
            port=int(os.environ.get("GALOCHKA_PORT", 8081)),
            expert_password=password,
            cassandra_url=os.environ.get("GALOCHKA_CASSANDRA_URL", ""),
            auto_sync=os.environ.get("GALOCHKA_AUTO_SYNC", "1").strip().lower() not in ("", "0", "false", "no", "off"),
            tz=os.environ.get("GALOCHKA_TZ", "").strip() or DEFAULT_TZ,
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "galochka.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"
