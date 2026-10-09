import csv
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryConfigTests(unittest.TestCase):
    def test_one_workflow_and_hosted_runners(self):
        workflows = list((ROOT / ".github/workflows").glob("*.yml"))
        workflows += list((ROOT / ".github/workflows").glob("*.yaml"))
        self.assertEqual([p.name for p in workflows], ["build-images.yml"])
        text = workflows[0].read_text()
        self.assertNotIn("self-hosted", text)
        self.assertIn("--matrix", text)
        self.assertIn("matrix.runner", text)
        self.assertIn("verify_artifacts.py", text)

    def test_manifest_has_matching_versions_for_both_architectures(self):
        with (ROOT / "镜像.md").open() as handle:
            rows = list(csv.reader(handle, delimiter="\t"))[1:]
        self.assertEqual(Counter(r[2] for r in rows), {"amd64": 15, "arm64": 15})
        self.assertEqual({(r[0], r[1]) for r in rows if r[2] == "amd64"},
                         {(r[0], r[1]) for r in rows if r[2] == "arm64"})
        self.assertTrue(all(len(r) == 9 and r[3] == "default" and r[7] == "YES" for r in rows))
        self.assertEqual(len({tuple(r[:4]) for r in rows}), 30)
