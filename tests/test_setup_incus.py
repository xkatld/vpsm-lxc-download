import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup-incus.sh"
REMOTE_LIST = ["incus", "remote", "list", "--format=json"]
REMOTE_ADD = [
    "incus", "remote", "add", "images", "https://images.linuxcontainers.org",
    "--protocol=simplestreams", "--public",
]

# This stub never executes a command. Its allowlist also makes unexpected
# privileged operations fail instead of silently accepting them.
SUDO_STUB = textwrap.dedent(r'''
    import json
    import os
    import sys
    from pathlib import Path

    args = sys.argv[1:]
    root = Path(os.environ["INCUS_TEST_ROOT"])
    stdin_data = sys.stdin.read()
    with (root / "commands.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"args": args, "stdin": stdin_data}) + "\n")

    def fail(message, status):
        print(message, file=sys.stderr)
        sys.exit(status)

    if Path(sys.argv[0]).name != "sudo":
        fail("privileged command bypassed sudo stub", 99)

    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    state = root / "remotes.json"
    allowed = [
        ["apt-get", "update"],
        ["DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "curl", "ca-certificates"],
        ["install", "-d", "-m", "0755", "/etc/apt/keyrings"],
        ["curl", "-fsSL", "https://pkgs.zabbly.com/key.asc", "-o", "/etc/apt/keyrings/zabbly.asc"],
        ["chmod", "0644", "/etc/apt/keyrings/zabbly.asc"],
        ["tee", "/etc/apt/sources.list.d/zabbly-incus-stable.list"],
        ["DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
         "incus", "openssh-client", "sshpass", "openssl", "ca-certificates"],
        ["systemctl", "start", "incus"],
        ["timeout", "120", "incus", "admin", "waitready"],
        ["incus", "admin", "init", "--preseed"],
        ["iptables", "-I", "FORWARD", "1", "-i", "incusbr0", "-j", "ACCEPT"],
        ["iptables", "-I", "FORWARD", "1", "-o", "incusbr0", "-m", "conntrack",
         "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["incus", "version"],
        ["incus", "storage", "list"],
        ["incus", "network", "list"],
    ]
    if args == ["incus", "remote", "list", "--format=json"]:
        if config["list_failure"]:
            fail("simulated remote list failure", 23)
        if config["malformed_json"]:
            print("{invalid JSON")
        else:
            print(state.read_text(encoding="utf-8"))
    elif args == ["incus", "remote", "add", "images",
                  "https://images.linuxcontainers.org", "--protocol=simplestreams", "--public"]:
        if config["add_failure"]:
            fail("simulated remote add failure", 37)
        remotes = json.loads(state.read_text(encoding="utf-8"))
        if "images" in remotes:
            fail("images remote already exists", 38)
        remotes["images"] = {
            "Addr": "https://images.linuxcontainers.org",
            "Protocol": "simplestreams",
            "Public": True,
        }
        state.write_text(json.dumps(remotes), encoding="utf-8")
    elif args == ["incus", "version"]:
        print("Client version: " + config["incus_version"])
        print("Server version: " + config["incus_version"])
    elif args in allowed:
        pass
    else:
        fail("unsupported sudo command: " + " ".join(args), 99)
''')


def image_remote():
    return {
        "Addr": "https://images.linuxcontainers.org",
        "Protocol": "simplestreams",
        "Public": True,
    }


class SetupIncusTests(unittest.TestCase):
    def run_setup(self, remotes, *, list_failure=False, malformed_json=False,
                  add_failure=False, incus_version="7.5.1"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "sudo"
            stub.write_text(f"#!{sys.executable}\n" + SUDO_STUB, encoding="utf-8")
            stub.chmod(0o755)
            # Guard accidental direct calls as well; none may reach the host.
            for command in ("incus", "apt-get", "systemctl", "iptables", "timeout"):
                (bin_dir / command).symlink_to(stub)
            (root / "config.json").write_text(json.dumps({
                "list_failure": list_failure,
                "malformed_json": malformed_json,
                "add_failure": add_failure,
                "incus_version": incus_version,
            }), encoding="utf-8")
            (root / "remotes.json").write_text(json.dumps(remotes), encoding="utf-8")
            env = os.environ.copy()
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", os.defpath)
            env["INCUS_TEST_ROOT"] = str(root)
            result = subprocess.run(
                ["bash", str(SCRIPT)], cwd=root, env=env,
                input="", capture_output=True, text=True, timeout=15,
            )
            records = [json.loads(line) for line in
                       (root / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
            state = json.loads((root / "remotes.json").read_text(encoding="utf-8"))

        commands = [record["args"] for record in records]
        self.assertTrue(commands, result.stderr)
        self.assertFalse(
            any(command[:3] == ["incus", "remote", "get-url"] for command in commands),
            "setup must not invoke the unsupported Incus remote get-url command",
        )
        self.assertNotIn("unsupported sudo command:", result.stderr)
        self.assertNotIn("privileged command bypassed sudo stub", result.stderr)
        return result, commands, records, state

    def assert_success(self, result, commands):
        self.assertEqual(result.returncode, 0, result.stderr)
        # Both discovery and final diagnostics must use the supported command.
        self.assertEqual(commands.count(REMOTE_LIST), 2)
        self.assertEqual(commands[-4:], [
            ["incus", "version"], REMOTE_LIST,
            ["incus", "storage", "list"], ["incus", "network", "list"],
        ])

    def assert_stopped(self, result, commands, remotes, state):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands.count(REMOTE_LIST), 1)
        self.assertNotIn(["incus", "version"], commands)
        self.assertEqual(state, remotes)

    def test_existing_images_remote_is_not_added(self):
        remotes = {"images": image_remote()}
        result, commands, records, state = self.run_setup(remotes)
        self.assert_success(result, commands)
        self.assertNotIn(REMOTE_ADD, commands)
        self.assertEqual(state, remotes)
        init_records = [record for record in records
                        if record["args"] == ["incus", "admin", "init", "--preseed"]]
        self.assertEqual(len(init_records), 1)
        preseed = init_records[0]["stdin"]
        for expected in (
            'images.auto_update_interval: "0"', "- name: incusbr0",
            "ipv4.address: auto", 'ipv4.nat: "true"', "ipv6.address: none",
            "driver: dir", "pool: default", "network: incusbr0", "type: nic",
        ):
            with self.subTest(preseed=expected):
                self.assertIn(expected, preseed)

    def test_missing_images_remote_is_added_once(self):
        for remotes in ({}, {"local": {
            "Addr": "unix://", "Protocol": "incus", "Public": False,
        }}):
            with self.subTest(remotes=remotes):
                result, commands, _, state = self.run_setup(remotes)
                self.assert_success(result, commands)
                self.assertEqual(commands.count(REMOTE_ADD), 1)
                self.assertLess(commands.index(REMOTE_LIST), commands.index(REMOTE_ADD))
                self.assertEqual(state, {**remotes, "images": image_remote()})

    def test_near_match_remote_names_do_not_count_as_images(self):
        for name in ("myimages", "images-backup", "Images"):
            with self.subTest(name=name):
                # Values deliberately match images; only the dictionary key counts.
                remotes = {name: image_remote()}
                result, commands, _, state = self.run_setup(remotes)
                self.assert_success(result, commands)
                self.assertEqual(commands.count(REMOTE_ADD), 1)
                self.assertEqual(state, {**remotes, "images": image_remote()})

    def test_remote_list_failure_aborts_without_adding(self):
        result, commands, _, state = self.run_setup({}, list_failure=True)
        self.assert_stopped(result, commands, {}, state)
        self.assertNotIn(REMOTE_ADD, commands)
        self.assertIn("simulated remote list failure", result.stderr)
        self.assertEqual(commands[-1], REMOTE_LIST)

    def test_malformed_remote_json_aborts_without_adding(self):
        result, commands, _, state = self.run_setup({}, malformed_json=True)
        self.assert_stopped(result, commands, {}, state)
        self.assertNotIn(REMOTE_ADD, commands)
        self.assertIn("JSONDecodeError", result.stderr)
        self.assertEqual(commands[-1], REMOTE_LIST)

    def test_remote_add_failure_is_propagated(self):
        result, commands, _, state = self.run_setup({}, add_failure=True)
        self.assert_stopped(result, commands, {}, state)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(commands.count(REMOTE_ADD), 1)
        self.assertIn("simulated remote add failure", result.stderr)
        self.assertEqual(commands[-1], REMOTE_ADD)

    def test_zabbly_repository_installs_incus_75(self):
        result, commands, records, state = self.run_setup({"images": image_remote()})
        self.assert_success(result, commands)
        self.assertEqual(commands.count(["apt-get", "update"]), 2)
        self.assertIn(["install", "-d", "-m", "0755", "/etc/apt/keyrings"], commands)
        self.assertIn(["chmod", "0644", "/etc/apt/keyrings/zabbly.asc"], commands)
        self.assertIn(
            ["curl", "-fsSL", "https://pkgs.zabbly.com/key.asc", "-o", "/etc/apt/keyrings/zabbly.asc"],
            commands,
        )
        tee_records = [record for record in records
                       if record["args"] == ["tee", "/etc/apt/sources.list.d/zabbly-incus-stable.list"]]
        self.assertEqual(len(tee_records), 1)
        self.assertIn("https://pkgs.zabbly.com/incus/stable noble main", tee_records[0]["stdin"])
        self.assertIn("signed-by=/etc/apt/keyrings/zabbly.asc", tee_records[0]["stdin"])

    def test_older_incus_version_aborts(self):
        result, commands, _, state = self.run_setup({"images": image_remote()}, incus_version="6.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("需要 Incus 7.5", result.stderr)
        self.assertEqual(commands[-1], ["incus", "version"])


if __name__ == "__main__":
    unittest.main()
