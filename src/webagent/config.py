from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    mcp_url: str = "http://127.0.0.1:8931/mcp"
    data_dir: Path = Path("var")

    @classmethod
    def from_env(cls):
        # Only the explicitly selected working directory's env, never a parent project.
        load_dotenv(Path.cwd() / ".env", override=False)
        return cls(api_key=os.getenv("LLM_API_KEY", ""), base_url=os.getenv("LLM_BASE_URL", cls.base_url),
                   model=os.getenv("LLM_MODEL", ""), mcp_url=os.getenv("WEBAGENT_MCP_URL", cls.mcp_url),
                   data_dir=Path(os.getenv("WEBAGENT_DATA_DIR", "var")))
