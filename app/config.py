from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://opsagent:opsagent_local@localhost:54329/opsagent"
    mock_order_url: str = "http://127.0.0.1:9001"
    mock_llm: bool = True
    confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    max_steps: int = Field(default=8, ge=1, le=100)
    run_timeout_seconds: int = Field(default=900, ge=1, le=86400)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=30)
    worker_enabled: bool = True
    worker_interval_seconds: float = Field(default=0.25, ge=0.01)
