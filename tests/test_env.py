"""验证启动入口加载项目配置，以及平台环境变量的优先级。"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

from main import main
from src.llm.client import LLMClient


class EnvironmentTests(unittest.TestCase):
    def test_paper_directory_uses_data_path_with_platform_precedence(self):
        for platform_value, expected in ((None, "/local/data/papers"), ("/platform/data", "/platform/data/papers")):
            environment = {} if platform_value is None else {"DATA_PATH": platform_value}
            with self.subTest(platform=platform_value), patch.dict(os.environ, environment, clear=True):
                with patch("main.dotenv_values", return_value={"DATA_PATH": "/local/data"}):
                    with patch("main.ResearchPilotRuntime") as runtime:
                        main()
                handler = runtime.call_args.args[0].handlers["Literature_Retrieval"]
                self.assertEqual(handler.papers_directory, Path(expected))

    def test_startup_loads_project_env_from_another_directory(self):
        env_path = Path(__file__).resolve().parents[1] / ".env"
        values = dotenv_values(env_path)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with patch("main.ResearchPilotRuntime") as runtime:
                    previous = Path.cwd()
                    try:
                        os.chdir(directory)
                        main()
                    finally:
                        os.chdir(previous)
                    dispatcher = runtime.call_args.args[0].dispatcher
                    self.assertEqual(dispatcher.workspace_root, Path(values["RESEARCH_RESULT_ROOT"]).resolve())
                    self.assertEqual(dispatcher.team, values["TEAM_NAME"])
                    self.assertEqual(dispatcher.team_directory, dispatcher.workspace_root / "data" / dispatcher.team)

    def test_startup_preserves_platform_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "RESEARCH_RESULT_ROOT": directory,
                "RESEARCH_WORKSPACE_TEAM": "platform-team",
                "TEAM_NAME": "external-local-team",
            }, clear=True):
                with patch("main.ResearchPilotRuntime") as runtime:
                    main()
                    dispatcher = runtime.call_args.args[0].dispatcher
                    self.assertEqual(dispatcher.workspace_root, Path(directory).resolve())
                    self.assertEqual(dispatcher.team, "platform-team")
                    self.assertEqual(os.environ["TEAM_NAME"], "external-local-team")

    def test_startup_does_not_let_file_model_variables_shadow_platform_aliases(self):
        file_values = {
            "CHALLENGE_LLM_BASE_URL": "https://file.invalid/v1",
            "CHALLENGE_LLM_API_KEY": "file-key", "CHALLENGE_LLM_MODEL": "file-model",
        }
        with patch.dict(os.environ, {
            "PLATFORM_LLM_URL": "https://platform.invalid/v1",
            "PLATFORM_API_KEY": "platform-key", "AGENT_MODEL_NAME": "platform-model",
        }, clear=True):
            with patch("main.dotenv_values", return_value=file_values):
                with patch("main.ResearchPilotRuntime"):
                    main()
            self.assertNotIn("CHALLENGE_LLM_BASE_URL", os.environ)
            client = LLMClient()
            self.assertEqual(client.base_url, "https://platform.invalid/v1")
            self.assertEqual(client._api_key, "platform-key")
            self.assertEqual(client.model, "platform-model")

    def test_startup_loads_missing_model_settings_for_client(self):
        file_values = {
            "CHALLENGE_LLM_BASE_URL": "https://file.invalid/v1",
            "CHALLENGE_LLM_API_KEY": "file-key", "CHALLENGE_LLM_MODEL": "file-model",
            "AGENT_LLM_TIMEOUT_S": "120",
        }
        with patch.dict(os.environ, {}, clear=True):
            with patch("main.dotenv_values", return_value=file_values):
                with patch("main.ResearchPilotRuntime"):
                    main()
            client = LLMClient()
            self.assertEqual(client.base_url, "https://file.invalid/v1")
            self.assertEqual(client._api_key, "file-key")
            self.assertEqual(client.model, "file-model")
            self.assertEqual(client.timeout_s, 120.0)

    def test_startup_fills_only_missing_model_settings(self):
        file_values = {
            "CHALLENGE_LLM_BASE_URL": "https://file.invalid/v1",
            "CHALLENGE_LLM_API_KEY": "file-key", "CHALLENGE_LLM_MODEL": "file-model",
        }
        with patch.dict(os.environ, {
            "PLATFORM_LLM_URL": "https://platform.invalid/v1", "PLATFORM_API_KEY": "platform-key",
        }, clear=True):
            with patch("main.dotenv_values", return_value=file_values):
                with patch("main.ResearchPilotRuntime"):
                    main()
            client = LLMClient()
            self.assertEqual(client.base_url, "https://platform.invalid/v1")
            self.assertEqual(client._api_key, "platform-key")
            self.assertEqual(client.model, "file-model")
            self.assertEqual(client.timeout_s, 900.0)


if __name__ == "__main__":
    unittest.main()
