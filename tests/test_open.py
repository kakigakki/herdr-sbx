"""End-to-end tests: the plugin entry points run as subprocesses with fake herdr / sbx / fzf
first on PATH, against real git repositories in a temporary directory."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.log = self.root / "calls.log"
        self.herdr_state = self.root / "herdr.json"
        self.sbx_state = self.root / "sbx.json"
        self.plugin_state = self.root / "state"
        self.plugin_state.mkdir()
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init",
            cwd=self.repo)
        self.herdr({"panes": {}})

    def worktree(self, name):
        path = self.root / name
        git("worktree", "add", "-q", str(path), cwd=self.repo)
        return path

    def herdr(self, state):
        self.herdr_state.write_text(json.dumps(state))

    def focused(self, cwd, *, workspace="w1", label="app", pane="w1:p1"):
        self.herdr({"panes": {pane: {"pane_id": pane, "cwd": str(cwd), "foreground_cwd": str(cwd)}}})
        self.context = {"workspace_id": workspace, "workspace_label": label, "focused_pane_id": pane,
                        "focused_pane_cwd": str(cwd), "invocation_source": "keybinding"}

    def sandboxes(self, *items):
        self.sbx_state.write_text(json.dumps([
            {"name": name, "status": status, "workspaces": [str(ws)]} for name, ws, status in items]))

    def run(self, *args, pick=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update({
            "PATH": f"{FAKES}:{env['PATH']}",
            "FAKE_LOG": str(self.log), "FAKE_HERDR": str(self.herdr_state), "FAKE_SBX": str(self.sbx_state),
            "HERDR_PLUGIN_ID": "herdr-sbx", "HERDR_PLUGIN_STATE_DIR": str(self.plugin_state),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(getattr(self, "context", {})),
        })
        if pick:
            env["FAKE_FZF_PICK"] = pick
        return subprocess.run([sys.executable, "-m", "herdr_sbx", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30)

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r[1:] for r in rows if tool in (None, r[0])]

    def herdr_calls(self, *prefix):
        return [c for c in self.calls("herdr") if c[:len(prefix)] == list(prefix)]


class OpenSingleSandbox(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)

    def test_opens_the_only_matching_sandbox_in_a_new_tab_with_the_agent_hint(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("other-1", self.f.root / "other", "running"))

        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("tab", "create"), [[
            "tab", "create", "--workspace", "w1", "--cwd", str(self.f.repo), "--label", "app-1", "--focus"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [
            ["pane", "run", "w1:p9", "HERDR_AGENT=claude sbx run --name app-1"]])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_a_linked_worktree_resolves_to_the_main_repository(self):
        wt = self.f.worktree("app-pr-1")
        self.f.focused(wt / ".")
        self.f.sandboxes(("app-1", self.f.repo, "stopped"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "run"), [
            ["pane", "run", "w1:p9", "HERDR_AGENT=claude sbx run --name app-1"]])

    def test_a_subdirectory_resolves_to_the_repository_root(self):
        sub = self.f.repo / "src"
        sub.mkdir()
        self.f.focused(sub)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(len(self.f.herdr_calls("pane", "run")), 1)

    def test_read_only_suffix_on_the_workspace_is_ignored(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", f"{self.f.repo}:ro", "running"))

        self.f.run("open")

        self.assertEqual(len(self.f.herdr_calls("pane", "run")), 1)

    def test_no_matching_sandbox_only_notifies(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("other-1", self.f.root / "other", "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("tab", "create"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn(str(self.f.repo), " ".join(note))
        self.assertEqual([c for c in self.f.calls("sbx") if c[:1] != ["ls"]], [])

    def test_outside_a_git_repository_only_notifies(self):
        plain = self.f.root / "plain"
        plain.mkdir()
        self.f.focused(plain)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("tab", "create"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_sbx_failure_only_notifies(self):
        self.f.focused(self.f.repo)  # no sandbox state file: the fake sbx fails

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("tab", "create"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn("sbx", " ".join(note))


class PickAmongSandboxes(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-2", self.f.repo, "stopped"), ("app-3", self.f.repo, "running"),
                         ("app-1", self.f.repo, "stopped"), ("other-1", self.f.root / "other", "running"))

    def offered(self):
        return [line.split()[0] for line in self.f.calls("fzf")[-1]]

    def test_several_matches_open_the_picked_sandbox(self):
        out = self.f.run("open", pick="app-2")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.f.herdr_calls("plugin", "pane", "open")), 1)
        self.assertEqual(self.f.herdr_calls("pane", "run"), [
            ["pane", "run", "w1:p9", "HERDR_AGENT=claude sbx run --name app-2"]])

    def test_the_picker_lists_running_first_then_by_name_with_status(self):
        self.f.run("open", pick="app-1")

        self.assertEqual(self.offered(), ["app-3", "app-1", "app-2"])
        self.assertIn("running", self.f.calls("fzf")[-1][0])

    def test_escape_opens_nothing(self):
        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("tab", "create"), [])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_the_last_choice_of_the_workspace_comes_first_next_time(self):
        self.f.run("open", pick="app-2")
        self.f.run("open", pick="app-2")
        self.assertEqual(self.offered()[0], "app-2")

        self.f.focused(self.f.repo, workspace="w2", label="app-alt", pane="w2:p1")
        self.f.run("open", pick="app-1")
        self.assertEqual(self.offered()[0], "app-3")


if __name__ == "__main__":
    unittest.main()
