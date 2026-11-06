"""核对本地打榜挂载目录与 explanation.md 的目录约定。"""

import hashlib
import json
from pathlib import Path
import unittest

from src.dispatcher import Dispatcher


class LeaderboardLayoutTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.data_root = self.project / "test_data"
        self.output_root = self.project / "test_output"
        self.team_root = self.output_root / "data/JTI"

    def test_mount_roots_contain_only_contest_directories(self):
        names = lambda directory: {path.name for path in directory.iterdir() if not path.name.startswith(".")}
        self.assertEqual(names(self.data_root), {"papers", "experiments"})
        self.assertEqual(names(self.output_root), {"data"})
        self.assertEqual(names(self.output_root / "data"), {"JTI", "expert", "figure"})
        for directory in (self.team_root / "work", self.output_root / "data/expert", self.output_root / "data/figure/JTI"):
            self.assertTrue(directory.is_dir())
        self.assertEqual(names(self.data_root / "experiments"), {"run_experiment.py", "D-01", "D-02", "D-05"})

    def test_assigned_filenames_and_dispatch_paths_match_current_round(self):
        record = json.loads((self.team_root / "dispatch.json").read_bytes())
        self.assertEqual(record["team"], "JTI")
        self.assertEqual(len(record["case_ids"]), 11)
        self.assertEqual(
            {path.name for path in (self.team_root / "assigned").iterdir()},
            {f"{case_id}.json" for case_id in record["case_ids"]},
        )
        dispatcher = Dispatcher(self.output_root, "JTI")
        for entry in record["cases"]:
            with self.subTest(case_id=entry["case_id"]):
                self.assertEqual(entry["question_file"], f"data/JTI/assigned/{entry['case_id']}.json")
                payload = json.loads((self.output_root / entry["question_file"]).read_bytes())
                self.assertEqual(dispatcher.resolve(payload), payload)

    def test_public_experiment_files_keep_official_names_and_contents(self):
        source = self.project / "docs/research-experiments-20261020-source-v1"
        self.assertEqual(
            (self.data_root / "experiments/run_experiment.py").read_bytes(),
            (source / "run_experiment.py").read_bytes(),
        )
        for case_id in ("D-01", "D-02", "D-05"):
            directory = self.data_root / "experiments" / case_id
            manifest = json.loads((directory / "manifest.json").read_bytes())
            for filename, digest in manifest["files"].items():
                with self.subTest(case_id=case_id, filename=filename):
                    content = (directory / filename).read_bytes()
                    self.assertEqual(content, (source / case_id / filename).read_bytes())
                    self.assertEqual(hashlib.sha256(content).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
