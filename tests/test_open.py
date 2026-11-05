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


def launch(name):
    return f'HERDR_AGENT=claude sbx run --name {name} && herdr pane close "$HERDR_PANE_ID"'


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
        self.workspaces = [{"workspace_id": "w1", "label": "app"}]
        self.herdr({})

    def worktree(self, name):
        path = self.root / name
        git("worktree", "add", "-q", str(path), cwd=self.repo)
        return path

    def herdr(self, state):
        self.herdr_state.write_text(json.dumps({"workspaces": self.workspaces, **state}))

    def sandbox_workspace(self, name, workspace_id="w5"):
        """An existing workspace this plugin made for sandbox `name`."""
        self.workspaces.append({"workspace_id": workspace_id, "label": name, "tokens": {"herdr_sbx": name}})

    def focused(self, cwd, *, workspace="w1", label="app", pane="w1:p1", size=(240, 60)):
        self.herdr({"panes": {pane: {"pane_id": pane, "cwd": str(cwd), "foreground_cwd": str(cwd)}},
                    "rects": {pane: {"width": size[0], "height": size[1], "x": 0, "y": 0}}})
        self.context = {"workspace_id": workspace, "workspace_label": label, "focused_pane_id": pane,
                        "focused_pane_cwd": str(cwd), "invocation_source": "keybinding"}

    def sandboxes(self, *items):
        self.sbx_state.write_text(json.dumps([
            {"name": name, "agent": agent, "status": status, "workspaces": [str(ws)]}
            for name, ws, status, *rest in items for agent in [rest[0] if rest else "claude"]]))

    def run(self, *args, pick=None, without_sbx=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        fakes = FAKES
        if without_sbx:
            fakes = self.root / "fakes-without-sbx"
            fakes.mkdir(exist_ok=True)
            for tool in ("herdr", "fzf"):
                if not (fakes / tool).exists():
                    (fakes / tool).symlink_to(FAKES / tool)
        env.update({
            # Without sbx, PATH must not reach a real sbx either.
            "PATH": f"{fakes}:/usr/bin:/bin" if without_sbx else f"{fakes}:{env['PATH']}",
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

    def launched(self):
        """Sandbox names started with `pane run`, in order."""
        return [c[3].split("--name ")[1].split(" ")[0] for c in self.herdr_calls("pane", "run")]


class OpenSandboxWorkspace(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)

    def test_a_new_workspace_named_after_the_sandbox_runs_claude_with_the_agent_hint(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("other-1", self.f.root / "other", "running"))

        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        ws_dir = self.f.plugin_state / "ws" / "app-1"
        self.assertTrue(ws_dir.is_dir())
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [
            ["workspace", "create", "--cwd", str(ws_dir), "--label", "app-1", "--focus"]])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [
            ["workspace", "report-metadata", "w9", "--source", "herdr-sbx", "--token", "herdr_sbx=app-1"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [["pane", "run", "w9:p1", launch("app-1")]])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_an_existing_sandbox_workspace_is_only_focused(self):
        self.f.sandbox_workspace("app-1")
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [["workspace", "focus", "w5"]])
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])

    def test_inside_a_sandbox_workspace_another_claude_joins_the_same_sandbox(self):
        self.f.sandbox_workspace("app-2")
        self.f.focused(self.f.plugin_state / "ws" / "app-2", workspace="w5", label="app-2", pane="w5:p1")
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("app-2", self.f.repo, "running"))

        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("plugin", "pane", "open"), [])
        self.assertEqual(self.f.herdr_calls("pane", "split"), [[
            "pane", "split", "w5:p1", "--direction", "right", "--cwd", str(self.f.plugin_state / "ws" / "app-2"),
            "--focus"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [["pane", "run", "w9:p2", launch("app-2")]])

    def test_a_narrow_pane_is_split_downward(self):
        self.f.sandbox_workspace("app-1")
        self.f.focused(self.f.repo, workspace="w5", pane="w5:p1", size=(100, 60))
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        [split] = self.f.herdr_calls("pane", "split")
        self.assertEqual(split[split.index("--direction") + 1], "down")

    def test_without_its_token_a_sandbox_workspace_is_recognized_by_its_directory(self):
        self.f.focused(self.f.plugin_state / "ws" / "app-2", workspace="w7", label="app-2", pane="w7:p1")
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("app-2", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("plugin", "pane", "open"), [])
        self.assertEqual(self.f.launched(), ["app-2"])

    def test_a_removed_sandbox_is_not_started_from_its_workspace(self):
        self.f.sandbox_workspace("app-2")
        self.f.focused(self.f.plugin_state / "ws" / "app-2", workspace="w5", label="app-2", pane="w5:p1")
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "split"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn("app-2", " ".join(note))

    def test_an_unsafe_name_in_the_workspace_token_is_refused(self):
        self.f.sandbox_workspace("app-1; touch pwned")
        self.f.focused(self.f.repo, workspace="w5", pane="w5:p1")
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_a_linked_worktree_resolves_to_the_main_repository(self):
        wt = self.f.worktree("app-pr-1")
        self.f.focused(wt / ".")
        self.f.sandboxes(("app-1", self.f.repo, "stopped"))

        self.f.run("open")

        self.assertEqual(self.f.launched(), ["app-1"])

    def test_a_subdirectory_resolves_to_the_repository_root(self):
        sub = self.f.repo / "src"
        sub.mkdir()
        self.f.focused(sub)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.launched(), ["app-1"])

    def test_read_only_suffix_on_the_workspace_is_ignored(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1", f"{self.f.repo}:ro", "running"))

        self.f.run("open")

        self.assertEqual(self.f.launched(), ["app-1"])

    def test_sandboxes_of_other_agents_are_not_candidates(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-codex", self.f.repo, "running", "codex"), ("app-1", self.f.repo, "stopped"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("plugin", "pane", "open"), [])
        self.assertEqual(self.f.launched(), ["app-1"])

    def test_no_matching_sandbox_only_notifies(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("other-1", self.f.root / "other", "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn(str(self.f.repo), " ".join(note))
        self.assertEqual([c for c in self.f.calls("sbx") if c[:1] != ["ls"]], [])

    def test_outside_a_git_repository_only_notifies(self):
        plain = self.f.root / "plain"
        plain.mkdir()
        self.f.focused(plain)
        self.f.sandboxes(("app-1", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_sbx_failure_only_notifies(self):
        self.f.focused(self.f.repo)  # no sandbox state file: the fake sbx fails

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn("sbx", " ".join(note))

    def test_malformed_sbx_output_only_notifies(self):
        self.f.focused(self.f.repo)
        self.f.sbx_state.write_text('{"not": "a list"}')

        out = self.f.run("open")

        self.assertEqual(out.stderr, "")
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_a_sandbox_name_unsafe_for_the_shell_is_refused(self):
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-1; touch pwned", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)


class Launch(unittest.TestCase):
    """`launch`: what the review / watch / fix-ci scripts call to start Claude in a sandbox."""

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        self.f.sandboxes(("app-1", self.f.repo, "stopped"), ("app-2", self.f.repo, "running"))
        self.setup = self.f.root / "setup.sh"
        self.setup.write_text("echo fetching >&2\necho noise\necho /vm/review-worktrees/app-pr-7\n")
        self.left = self.f.root / "left.md"
        self.left.write_text("review in {{WORKDIR}}")
        self.right = self.f.root / "right.md"
        self.right.write_text("explain {{WORKDIR}}")

    def launch(self, *extra):
        return self.f.run("launch", "--repo", str(self.f.repo), "--label", "🔍app#7", *extra)

    def prompt_of(self, call):
        """The prompt file a `pane run` passes to Claude, read back."""
        path = call[3].split('"$(cat ')[1].split(";")[0]
        return Path(path.strip("'")).read_text()

    def test_starts_one_claude_per_prompt_in_a_labelled_workspace_of_the_running_sandbox(self):
        out = self.launch("--setup-file", str(self.setup), "--prompt-file", str(self.left),
                          "--prompt-file", str(self.right))

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "app-2")
        ws_dir = self.f.plugin_state / "ws" / "🔍app#7"
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [
            ["workspace", "create", "--cwd", str(ws_dir), "--label", "🔍app#7", "--focus"]])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [
            ["workspace", "report-metadata", "w9", "--source", "herdr-sbx", "--token", "herdr_sbx=app-2"]])
        self.assertEqual(self.f.herdr_calls("pane", "split"), [[
            "pane", "split", "w9:p1", "--direction", "right", "--ratio", "0.6", "--cwd", str(ws_dir), "--no-focus"]])
        runs = self.f.herdr_calls("pane", "run")
        self.assertEqual([c[2] for c in runs], ["w9:p1", "w9:p2"])
        self.assertTrue(all(c[3].startswith("HERDR_AGENT=claude sbx run --name app-2 -- ") for c in runs))
        self.assertEqual([self.prompt_of(c) for c in runs],
                         ["review in /vm/review-worktrees/app-pr-7", "explain /vm/review-worktrees/app-pr-7"])
        self.assertEqual(self.f.calls("sbx")[-1][:5], ["exec", "-i", "app-2", "--", "bash"])

    def test_without_a_sandbox_it_exits_3_for_the_caller_to_fall_back(self):
        self.f.sandboxes(("other-1", self.f.root / "other", "running"))

        out = self.launch("--prompt-file", str(self.left))

        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_without_sbx_installed_it_also_exits_3(self):
        out = self.f.run("launch", "--repo", str(self.f.repo), "--label", "x", "--prompt-file", str(self.left),
                         without_sbx=True)

        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self.f.herdr_calls("notification"), [])

    def test_a_failing_setup_notifies_and_opens_nothing(self):
        self.setup.write_text("echo cannot fetch >&2\nexit 4\n")

        out = self.launch("--setup-file", str(self.setup), "--prompt-file", str(self.left))

        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn("cannot fetch", " ".join(note))

    def test_the_open_key_does_not_jump_to_a_launched_workspace(self):
        self.f.workspaces.append({"workspace_id": "w6", "label": "🔍app#7", "tokens": {"herdr_sbx": "app-2"}})
        self.f.focused(self.f.repo)
        self.f.sandboxes(("app-2", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [])
        self.assertEqual(self.f.launched(), ["app-2"])


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
        self.assertEqual(self.f.launched(), ["app-2"])

    def test_the_picker_lists_running_first_then_by_name_with_status(self):
        self.f.run("open", pick="app-1")

        self.assertEqual(self.offered(), ["app-3", "app-1", "app-2"])
        self.assertIn("running", self.f.calls("fzf")[-1][0])

    def test_escape_opens_nothing(self):
        out = self.f.run("open")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
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
