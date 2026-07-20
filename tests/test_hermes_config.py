from __future__ import annotations

from pathlib import Path

import yaml

from investor_core.hermes_config import configure_investor_mcp


def test_configure_investor_mcp_preserves_profile_and_other_servers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": "deepseek-v4-pro",
                "mcp_servers": {
                    "other": {"command": "other-mcp"},
                    "investor_core": {
                        "url": "http://old.invalid",
                        "tools": {"include": ["system_health_get"]},
                        "env": {"PRESERVE_ME": "yes"},
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    configure_investor_mcp(
        config_path,
        project_root=tmp_path / "value-dca-agent",
        core_url="http://127.0.0.1:8710",
        task_name="ValueDCAInvestorCore",
    )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["model"] == "deepseek-v4-pro"
    assert result["mcp_servers"]["other"] == {"command": "other-mcp"}
    investor = result["mcp_servers"]["investor_core"]
    assert "url" not in investor
    assert investor["command"] == "uv"
    assert investor["args"][-2:] == ["run", "investor-mcp"]
    assert investor["tools"] == {"include": ["system_health_get"]}
    assert investor["env"] == {
        "PRESERVE_ME": "yes",
        "INVESTOR_CORE_BASE_URL": "http://127.0.0.1:8710",
        "INVESTOR_CORE_AUTOSTART": "true",
        "INVESTOR_CORE_WINDOWS_TASK_NAME": "ValueDCAInvestorCore",
    }
    assert config_path.with_suffix(".yaml.bak").exists()
