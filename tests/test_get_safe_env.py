from pathlib import Path

from utilities.get_safe_env import get_safe_env_value


def test_get_safe_env_value_blocks_non_allowlisted_keys(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")

    assert get_safe_env_value(env_path, "OPENAI_API_KEY") is None


def test_get_safe_env_value_redacts_mongo_password(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MONGO_URI=mongodb+srv://user:supersecret@cluster.mongodb.net/?retryWrites=true\n",
        encoding="utf-8",
    )

    value = get_safe_env_value(env_path, "MONGO_URI")
    assert value
    assert "supersecret" not in value
    assert "user" not in value
    assert value == "mongodb+srv://cluster.mongodb.net"


def test_get_safe_env_value_redacts_standard_mongo_uri(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MONGO_DNS_FALLBACK_URI="
        "mongodb://user:supersecret@host.mongodb.net:27017/?tls=true\n",
        encoding="utf-8",
    )

    value = get_safe_env_value(env_path, "MONGO_DNS_FALLBACK_URI")

    assert value == "mongodb://host.mongodb.net:27017"


def test_get_safe_env_value_returns_plain_value_for_non_secret(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("VON_DB_NAME=von_db\n", encoding="utf-8")

    assert get_safe_env_value(env_path, "VON_DB_NAME") == "von_db"
