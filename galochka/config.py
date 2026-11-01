import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8081
    expert_password: str = "experte"
    # Base URL of the sister app Cassandra (item catalog and reference photos).
    # Empty = not configured; the CSV import is then the only item source.
    cassandra_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("GALOCHKA_DATA", "./data")),
            port=int(os.environ.get("GALOCHKA_PORT", 8081)),
            expert_password=os.environ.get("GALOCHKA_EXPERT_PASSWORD", "experte"),
            cassandra_url=os.environ.get("GALOCHKA_CASSANDRA_URL", ""),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "galochka.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"
