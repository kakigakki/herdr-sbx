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
        self.ps = self.root / "ps.txt"
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

    def focused(self, cwd, *, workspace="w1", label="app", pane="w1:p1", size=(240, 60)):
        self.herdr({"panes": {pane: {"pane_id": pane, "cwd": str(cwd), "foreground_cwd": str(cwd)}},
                    "rects": {pane: {"width": size[0], "height": size[1], "x": 0, "y": 0}}})
        self.context = {"workspace_id": workspace, "workspace_label": label, "focused_pane_id": pane,
                        "focused_pane_cwd": str(cwd), "invocation_source": "keybinding"}

    def sandboxes(self, *items):
        self.sbx_state.write_text(json.dumps([
            {"name": name, "agent": agent, "status": status, "workspaces": [str(ws)]}
            for name, ws, status, *rest in items for agent in [rest[0] if rest else "claude"]]))

    def run(self, *args, pick=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update({
            "PATH": f"{FAKES}:{env['PATH']}",
            "FAKE_LOG": str(self.log), "FAKE_HERDR": str(self.herdr_state), "FAKE_SBX": str(self.sbx_state), "FAKE_PS": str(self.ps),
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

    def test_opens_the_only_matching_sandbox_in_a_split_pane_with_the_agent_hint(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("other-1", self.f.root / "other", "running"))

        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("pane", "split"), [[
            "pane", "split", "w1:p1", "--direction", "right", "--cwd", str(self.f.repo), "--focus"]])
        self.assertEqual(self.f.herdr_calls("pane", "rename"), [["pane", "rename", "w1:p9", "app-1"]])
        self.assertEqual(self.f.herdr_calls("pane", "report-metadata"), [
            ["pane", "report-metadata", "w1:p9", "--source", "herdr-sbx", "--token", "sbx=app-1"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [
            ["pane", "run", "w1:p9", "HERDR_AGENT=claude sbx run --name app-1"]])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_a_narrow_pane_is_split_downward(self):
        self.f.focused(self.f.repo, size=(100, 60))
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        [split] = self.f.herdr_calls("pane", "split")
        self.assertEqual(split[split.index("--direction") + 1], "down")

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

    def test_sandboxes_of_other_agents_are_not_candidates(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-codex", self.f.repo, "running", "codex"), ("app-1", self.f.repo, "stopped"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("plugin", "pane", "open"), [])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [
            ["pane", "run", "w1:p9", "HERDR_AGENT=claude sbx run --name app-1"]])

    def test_no_matching_sandbox_only_notifies(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("other-1", self.f.root / "other", "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn(str(self.f.repo), " ".join(note))
        self.assertEqual([c for c in self.f.calls("sbx") if c[:1] != ["ls"]], [])

    def test_outside_a_git_repository_only_notifies(self):
        plain = self.f.root / "plain"
        plain.mkdir()
        self.f.focused(plain)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_sbx_failure_only_notifies(self):
        self.f.focused(self.f.repo)  # no sandbox state file: the fake sbx fails

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn("sbx", " ".join(note))

    def test_malformed_sbx_output_only_notifies(self):
        self.f.focused(self.f.repo)
        self.f.sbx_state.write_text('{"not": "a list"}')

        out = self.f.run("open")

        self.assertEqual(out.stderr, "")
        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_a_sandbox_name_unsafe_for_the_shell_is_refused(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1; touch pwned", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)


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
        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_the_last_choice_of_the_workspace_comes_first_next_time(self):
        self.f.run("open", pick="app-2")
        self.f.run("open", pick="app-2")
        self.assertEqual(self.offered()[0], "app-2")

        self.f.focused(self.f.repo, workspace="w2", label="app-alt", pane="w2:p1")
        self.f.run("open", pick="app-1")
        self.assertEqual(self.offered()[0], "app-3")


class ReportToSpaces(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        other = self.f.root / "other"
        self.f.sandboxes(("app-2", self.f.repo, "stopped"), ("app-1", self.f.repo, "running"),
                         ("other-1", other, "running"))
        self.f.ps.write_text("\n".join([
            "/opt/homebrew/bin/sbx daemon start",
            "sbx exec app-1 -- sleep infinity",
            "sbx run --name app-1",
            "sbx run --name app-1",
            "sbx run --name=app-1",
            "grep sbx run --name app-1",
        ]))

        def agent(pane, status, sbx=None, agent="claude"):
            return {"pane_id": pane, "agent": agent, "agent_status": status, "tokens": {"sbx": sbx} if sbx else {}}
        self.f.herdr({
            "workspaces": [
                {"workspace_id": "w1", "label": "app", "worktree": {"checkout_path": str(self.f.repo)}},
                {"workspace_id": "w2", "label": "app-alt", "worktree": {"checkout_path": str(self.f.repo / "sub")}},
                {"workspace_id": "w3", "label": "loose"},
            ],
            "panes": {"w3:p1": {"pane_id": "w3:p1", "workspace_id": "w3", "cwd": str(other)}},
            "agents": [agent("w1:p2", "working", "app-1"), agent("w1:p3", "blocked", "app-1"),
                       agent("w1:p4", "idle", "app-1"), agent("w1:p5", "working"),
                       agent("w1:p6", "working", "app-1", agent="codex")],
        })

    def reported(self):
        out = {}
        for call in self.f.herdr_calls("workspace", "report-metadata"):
            ws = call[2]
            if "--token" in call:
                out[ws] = call[call.index("--token") + 1]
            elif "--clear-token" in call:
                out[ws] = None
        return out

    def test_only_the_workspace_of_the_creation_directory_shows_its_sandboxes(self):
        out = self.f.run("report")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.reported(), {
            "w1": "sbx=app-1● 3⡿1?1  app-2○",
            "w2": None,
            "w3": None,
        })

    def test_tokens_expire_when_the_job_stops(self):
        self.f.run("report")

        for call in self.f.herdr_calls("workspace", "report-metadata"):
            self.assertIn("--ttl-ms", call)

    def test_failures_are_silent(self):
        self.f.sbx_state.unlink()

        out = self.f.run("report")

        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.f.herdr_calls("notification"), [])


if __name__ == "__main__":
    unittest.main()
