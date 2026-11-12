"""End-to-end tests: the plugin entry points run as subprocesses with fake herdr / sbx / fzf
first on PATH, against real git repositories in a temporary directory."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit(*args, cwd):
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", *args, cwd=cwd)


def marked(workspace, name):
    """The report-metadata call that marks a sandbox's workspace."""
    return ["workspace", "report-metadata", workspace, "--source", "herdr-sbx", "--token", f"herdr_sbx={name}",
            "--token", "herdr_sbx_badge=sbx"]


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
        # Never the real herdr socket: a test that moves workspaces starts api_server().
        self.socket_path = self.root / "herdr.sock"
        self.repo = self.root / "app"
        self.repo.mkdir()
        git("init", "-q", "-b", "main", cwd=self.repo)
        commit("--allow-empty", "-m", "init", cwd=self.repo)
        self.workspaces = [{"workspace_id": "w1", "label": "app"}]
        self.herdr({})

    def repo_workspace(self, workspace_id="w1", label="app"):
        """Make the workspace `workspace_id` the one herdr opened on the repository's main checkout."""
        w = next(w for w in self.workspaces if w["workspace_id"] == workspace_id)
        w.update(label=label, worktree=self.worktree_info(self.repo, linked=False))
        self.herdr({})

    def worktree_info(self, path, linked=True):
        return {"repo_key": str(self.repo / ".git"), "repo_name": "app", "repo_root": str(self.repo),
                "checkout_path": str(path), "is_linked_worktree": linked}

    def api_server(self):
        """A fake herdr socket: records each request and answers with an empty result."""
        self.api_requests = []
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.socket_path))
        server.listen()

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    if not data:
                        continue
                    request = json.loads(data)
                    self.api_requests.append([request["method"], request["params"]])
                    conn.sendall((json.dumps({"id": request["id"], "result": {}}) + "\n").encode())

        threading.Thread(target=serve, daemon=True).start()
        return server

    def worktree(self, name):
        path = self.root / name
        git("worktree", "add", "-q", str(path), cwd=self.repo)
        return path

    def herdr(self, state):
        self.herdr_state.write_text(json.dumps({"workspaces": self.workspaces, **state}))

    def sandbox_workspace(self, name, workspace_id="w5", badge=True):
        """An existing workspace this plugin made for sandbox `name` (without the badge: by an earlier version)."""
        tokens = {"herdr_sbx": name, **({"herdr_sbx_badge": "sbx"} if badge else {})}
        self.workspaces.append({"workspace_id": workspace_id, "label": name, "tokens": tokens})

    def focused(self, cwd, *, workspace="w1", label="app", pane="w1:p1", size=(240, 60)):
        self.herdr({"panes": {pane: {"pane_id": pane, "cwd": str(cwd), "foreground_cwd": str(cwd)}},
                    "rects": {pane: {"width": size[0], "height": size[1], "x": 0, "y": 0}}})
        self.context = {"workspace_id": workspace, "workspace_label": label, "focused_pane_id": pane,
                        "focused_pane_cwd": str(cwd), "invocation_source": "keybinding"}

    def sandboxes(self, *items):
        self.sbx_state.write_text(json.dumps([
            {"name": name, "agent": agent, "status": status, "workspaces": [str(ws)]}
            for name, ws, status, *rest in items for agent in [rest[0] if rest else "claude"]]))

    def run(self, *args, pick=None, without_sbx=False, extra_env=None):
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
            "HERDR_SOCKET_PATH": str(self.socket_path),
        })
        if pick:
            env["FAKE_FZF_PICK"] = pick
        env.update(extra_env or {})
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
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [marked("w9", "app-1")])
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

    def test_without_its_token_a_launch_workspace_is_not_taken_for_a_sandbox(self):
        # Its directory is named after its label, which may be another sandbox's name.
        self.f.focused(self.f.plugin_state / "tasks" / "app-2", workspace="w7", label="app-2", pane="w7:p1")
        self.f.sandboxes(("app-1", self.f.repo, "running"), ("app-2", self.f.repo, "running"))

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

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

        self.assertNotIn("Traceback", out.stderr)
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
        ws_dir = self.f.plugin_state / "tasks" / "🔍app#7"
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [
            ["workspace", "create", "--cwd", str(ws_dir), "--label", "🔍app#7", "--focus"]])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [marked("w9", "app-2")])
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


class StartByName(unittest.TestCase):
    """`start <sandbox>`: what a host command that would run `sbx run --name <sandbox>` calls instead."""

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        self.f.sandboxes(("app-1", self.f.repo, "stopped"), ("codex-1", self.f.repo, "running", "codex"))

    def test_opens_the_sandbox_workspace_with_claude_like_the_key(self):
        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [
            ["workspace", "create", "--cwd", str(self.f.plugin_state / "ws" / "app-1"), "--label", "app-1",
             "--focus"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [["pane", "run", "w9:p1", launch("app-1")]])

    def test_an_existing_sandbox_workspace_is_only_focused(self):
        self.f.sandbox_workspace("app-1")
        self.f.herdr({})

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [["workspace", "focus", "w5"]])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])

    def test_a_workspace_marked_without_the_badge_gets_it_when_focused(self):
        self.f.sandbox_workspace("app-1", badge=False)
        self.f.herdr({})

        self.f.run("start", "app-1")

        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [marked("w5", "app-1")])
        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [["workspace", "focus", "w5"]])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])

    def test_a_workspace_launch_made_in_the_same_sandbox_is_not_reused(self):
        self.f.workspaces.append({"workspace_id": "w6", "label": "🔍app#7", "tokens": {"herdr_sbx": "app-1"}})
        self.f.herdr({})

        self.f.run("start", "app-1")

        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [])
        self.assertEqual(self.f.launched(), ["app-1"])

    def test_an_unknown_or_non_claude_sandbox_exits_3_for_the_caller_to_run_sbx(self):
        for name in ("missing-1", "codex-1"):
            with self.subTest(name=name):
                out = self.f.run("start", name)

                self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self.f.herdr_calls(), [])

    def test_without_sbx_installed_it_also_exits_3(self):
        out = self.f.run("start", "app-1", without_sbx=True)

        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self.f.herdr_calls(), [])

    def test_a_name_unsafe_for_the_shell_is_refused(self):
        out = self.f.run("start", "app-1; touch pwned")

        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(self.f.calls("sbx"), [])

    def test_outside_a_plugin_action_it_uses_the_state_directory_herdr_gives_the_plugin(self):
        xdg = self.f.root / "xdg"

        self.f.run("start", "app-1", extra_env={"HERDR_PLUGIN_STATE_DIR": "", "XDG_STATE_HOME": str(xdg)})

        [create] = self.f.herdr_calls("workspace", "create")
        self.assertEqual(create[3], str(xdg / "herdr" / "plugins" / "herdr-sbx" / "ws" / "app-1"))

    def test_the_sandbox_name_is_required(self):
        self.assertEqual(self.f.run("start").returncode, 2)
        self.assertEqual(self.f.run("start", "a", "b").returncode, 2)


class NestedUnderTheRepositoryWorkspace(unittest.TestCase):
    """With the repository's workspace open, the sandbox's workspace becomes a worktree child of it."""

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        self.f.sandboxes(("app-1", self.f.repo, "running"))
        self.f.repo_workspace()
        self.server = self.f.api_server()
        self.addCleanup(self.server.close)
        self.checkout = self.f.plugin_state / "wt" / "app-1"

    def git_worktrees(self):
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=self.f.repo, capture_output=True,
                             text=True, check=True).stdout
        return [Path(line[len("worktree "):]).resolve() for line in out.splitlines() if line.startswith("worktree ")]

    def commit_a_file(self):
        (self.f.repo / "README").write_text("app")
        git("add", "README", cwd=self.f.repo)
        commit("-m", "readme", cwd=self.f.repo)

    def git_status(self):
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.checkout, capture_output=True, text=True,
                              check=True).stdout

    def test_start_opens_an_empty_linked_worktree_under_the_repository_workspace(self):
        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(self.checkout.resolve(), self.git_worktrees())
        self.assertEqual([p.name for p in self.checkout.iterdir()], [".git"])  # no files checked out
        self.assertEqual(self.f.herdr_calls("worktree", "open"), [
            ["worktree", "open", "--workspace", "w1", "--path", str(self.checkout), "--label", "app-1", "--focus"]])
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [marked("w9", "app-1")])
        self.assertEqual(self.f.herdr_calls("pane", "run"), [["pane", "run", "w9:p1", launch("app-1")]])

    def test_the_new_workspace_moves_right_after_the_repository_group(self):
        self.f.workspaces += [
            {"workspace_id": "w2", "label": "app-pr-1", "worktree": self.f.worktree_info(self.f.root / "pr-1")},
            {"workspace_id": "w3", "label": "other"},
            {"workspace_id": "w9", "label": "app-1", "worktree": self.f.worktree_info(self.checkout)}]
        self.f.herdr({})

        self.f.run("start", "app-1")

        self.assertEqual(self.f.api_requests, [
            ["workspace.move_block", {"workspace_ids": ["w9"], "before_workspace_id": "w3"}]])

    def test_a_parent_found_by_its_panes_still_keeps_its_worktrees_before_the_new_one(self):
        # herdr records the parent's checkout only when the worktree is opened: the move reads the list after it.
        parent = self.f.workspaces[0]
        tree = parent.pop("worktree")
        self.f.herdr({"panes": {"w1:p1": {"pane_id": "w1:p1", "workspace_id": "w1", "cwd": str(self.f.repo)}}})
        before = json.loads(self.f.herdr_state.read_text())
        before["workspaces"][0]["worktree"] = tree  # what `workspace list` answers after the open
        before["workspaces"] += [
            {"workspace_id": "w2", "label": "app-pr-1", "worktree": self.f.worktree_info(self.f.root / "pr-1")},
            {"workspace_id": "w3", "label": "other"}]
        state = self.f.root / "after-open.json"
        state.write_text(json.dumps(before))
        # The first `workspace list` (before the open) has no provenance, the later one has.
        self.f.herdr_state.write_text(json.dumps({**json.loads(self.f.herdr_state.read_text()),
                                                  "after_worktree_open": str(state)}))

        self.f.run("start", "app-1")

        self.assertEqual(self.f.api_requests, [
            ["workspace.move_block", {"workspace_ids": ["w9"], "before_workspace_id": "w3"}]])

    def test_the_open_key_in_the_repository_also_nests(self):
        self.f.focused(self.f.repo)

        self.f.run("open")

        self.assertEqual(len(self.f.herdr_calls("worktree", "open")), 1)
        self.assertEqual(self.f.launched(), ["app-1"])

    def test_git_status_in_the_empty_checkout_does_not_list_the_repositorys_files_as_deleted(self):
        self.commit_a_file()
        self.f.run("start", "app-1")

        self.assertEqual(self.git_status(), "")

    def test_the_empty_checkout_can_be_removed_without_force(self):
        self.commit_a_file()
        self.f.run("start", "app-1")

        out = subprocess.run(["git", "worktree", "remove", str(self.checkout)], cwd=self.f.repo, capture_output=True)

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(self.checkout.resolve(), self.git_worktrees())

    def test_a_checkout_made_before_the_files_were_hidden_is_quieted_when_reused(self):
        self.commit_a_file()
        self.checkout.parent.mkdir(parents=True)
        git("worktree", "add", "-q", "--no-checkout", "--detach", str(self.checkout), cwd=self.f.repo)
        self.assertNotEqual(self.git_status(), "")

        self.f.run("start", "app-1")

        self.assertEqual(self.git_status(), "")

    def test_the_checkout_of_an_earlier_run_is_reused(self):
        self.f.run("start", "app-1")
        self.f.run("start", "app-1")

        self.assertEqual(self.git_worktrees().count(self.checkout.resolve()), 1)
        self.assertEqual(len(self.f.herdr_calls("worktree", "open")), 2)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])

    def test_its_workspace_without_the_token_is_focused_not_started_again(self):
        # e.g. after a herdr restart: herdr answers with the workspace already open on the checkout.
        self.f.herdr({"worktree_open": {"already_open": True, "workspace_id": "w5"}})

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("pane", "run"), [])
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(self.f.herdr_calls("workspace", "report-metadata"), [marked("w5", "app-1")])
        self.assertEqual(self.f.herdr_calls("workspace", "focus"), [["workspace", "focus", "w5"]])
        self.assertEqual(self.f.api_requests, [])

    def test_inside_the_nested_workspace_without_its_token_another_claude_joins(self):
        self.f.focused(self.checkout, workspace="w7", label="app-1", pane="w7:p1")

        self.f.run("open")

        self.assertEqual(self.f.herdr_calls("worktree", "open"), [])
        self.assertEqual(self.f.launched(), ["app-1"])

    def test_a_failing_worktree_open_falls_back_to_a_plain_workspace(self):
        self.f.herdr({"worktree_open": {"fail": True}})

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [
            ["workspace", "create", "--cwd", str(self.f.plugin_state / "ws" / "app-1"), "--label", "app-1",
             "--focus"]])
        self.assertEqual(self.f.launched(), ["app-1"])

    def test_a_failure_after_the_nested_workspace_opened_is_reported_without_a_second_workspace(self):
        self.f.herdr({"worktree_open": {"workspace_id": "gone"}, "fail_pane_list": True})

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_a_sandbox_whose_workspace_is_a_subdirectory_nests_under_the_repository(self):
        sub = self.f.repo / "src"
        sub.mkdir()
        self.f.sandboxes(("app-1", sub, "running"))

        self.f.run("start", "app-1")
        self.f.run("start", "app-1")

        self.assertEqual(len(self.f.herdr_calls("worktree", "open")), 2)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])

    def test_a_checkout_whose_directory_was_removed_is_made_again(self):
        self.f.run("start", "app-1")
        subprocess.run(["rm", "-rf", str(self.checkout)], check=True)  # git still has it registered

        self.f.run("start", "app-1")

        self.assertEqual([p.name for p in self.checkout.iterdir()], [".git"])
        self.assertEqual(len(self.f.herdr_calls("worktree", "open")), 2)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])

    def test_a_submodules_checkout_is_reused(self):
        sub = self.f.root / "lib"
        sub.mkdir()
        git("init", "-q", "-b", "main", cwd=sub)
        commit("--allow-empty", "-m", "init", cwd=sub)
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "lib", cwd=self.f.repo)
        module = (self.f.repo / "lib").resolve()
        self.f.sandboxes(("app-1", module, "running"))
        self.f.workspaces[0]["worktree"] = self.f.worktree_info(module, linked=False)
        self.f.herdr({})

        self.f.run("start", "app-1")
        self.f.run("start", "app-1")

        self.assertEqual(len(self.f.herdr_calls("worktree", "open")), 2)
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])

    def test_a_pane_less_workspace_after_the_open_is_reported(self):
        self.f.herdr({"empty_pane_list": True})

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 1)
        self.assertNotIn("Traceback", out.stderr)
        self.assertEqual(len(self.f.herdr_calls("notification", "show")), 1)

    def test_something_else_in_the_checkouts_place_is_left_alone(self):
        self.checkout.mkdir(parents=True)
        (self.checkout / "keep.txt").write_text("mine")

        self.f.run("start", "app-1")

        self.assertEqual(self.f.herdr_calls("worktree", "open"), [])
        self.assertEqual((self.checkout / "keep.txt").read_text(), "mine")
        self.assertEqual(len(self.f.herdr_calls("workspace", "create")), 1)
        [note] = self.f.herdr_calls("notification", "show")
        self.assertIn(str(self.checkout), " ".join(note))

    def test_without_a_reachable_socket_it_still_opens_nested(self):
        self.server.close()
        self.f.socket_path.unlink()

        out = self.f.run("start", "app-1")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.f.launched(), ["app-1"])
        self.assertEqual(self.f.herdr_calls("workspace", "create"), [])

    def test_without_herdr_knowing_its_checkout_the_workspace_with_a_pane_in_the_repository_is_the_parent(self):
        del self.f.workspaces[0]["worktree"]
        self.f.workspaces.insert(0, {"workspace_id": "w2", "label": "elsewhere"})
        self.f.herdr({"panes": {
            "w2:p1": {"pane_id": "w2:p1", "workspace_id": "w2", "cwd": str(self.f.root)},
            "w1:p1": {"pane_id": "w1:p1", "workspace_id": "w1", "cwd": str(self.f.root),
                      "foreground_cwd": str(self.f.repo)},
        }})

        self.f.run("start", "app-1")

        [opened] = self.f.herdr_calls("worktree", "open")
        self.assertEqual(opened[:4], ["worktree", "open", "--workspace", "w1"])

    def test_a_pane_in_a_subdirectory_also_finds_the_parent_but_the_root_comes_first(self):
        del self.f.workspaces[0]["worktree"]
        self.f.workspaces.insert(0, {"workspace_id": "w2", "label": "app-src"})
        self.f.workspaces.append({"workspace_id": "w3", "label": "other-repo",
                                  "worktree": {"checkout_path": str(self.f.root / "other")}})
        src = self.f.repo / "src"
        src.mkdir()
        panes = {"w2:p1": {"pane_id": "w2:p1", "workspace_id": "w2", "cwd": str(src)},
                 "w3:p1": {"pane_id": "w3:p1", "workspace_id": "w3", "cwd": str(self.f.repo)}}
        self.f.herdr({"panes": panes})

        self.f.run("start", "app-1")
        [opened] = self.f.herdr_calls("worktree", "open")
        self.assertEqual(opened[3], "w2")  # w3 is in the root but herdr knows it as another checkout

        panes["w1:p1"] = {"pane_id": "w1:p1", "workspace_id": "w1", "cwd": str(self.f.repo)}
        self.f.herdr({"panes": panes})
        self.f.run("start", "app-1")
        self.assertEqual(self.f.herdr_calls("worktree", "open")[-1][3], "w1")

    def test_a_workspace_on_a_linked_worktree_of_the_repository_is_not_the_parent(self):
        self.f.workspaces[0]["worktree"] = self.f.worktree_info(self.f.root / "pr-1")
        self.f.herdr({})

        self.f.run("start", "app-1")

        self.assertEqual(self.f.herdr_calls("worktree", "open"), [])
        self.assertEqual(len(self.f.herdr_calls("workspace", "create")), 1)


class HeadlessLaunch(unittest.TestCase):
    """`launch --headless`: Claude runs in the sandbox without a workspace (mainline's own-PR review)."""

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.tmp.cleanup)
        self.f.sandboxes(("app-2", self.f.repo, "running"))
        self.workdir = self.f.root / "wt"
        self.setup = self.f.root / "setup.sh"
        self.result = self.f.root / "out" / "app-PR7.md"
        self.setup.write_text(f"mkdir -p {self.workdir} {self.result.parent}\necho {self.workdir}\n")
        self.prompt = self.f.root / "left.md"
        self.prompt.write_text("review in {{WORKDIR}}")
        self.log = self.f.root / "app-PR7.log"

    def launch(self, **env):
        return self.f.run("launch", "--repo", str(self.f.repo), "--label", "🔍app#7", "--setup-file", str(self.setup),
                          "--prompt-file", str(self.prompt), "--headless", "--log", str(self.log),
                          "--fetch", str(self.result), extra_env=env)

    def wait_for_result(self):
        # The fake VM shares the host's paths, so the file exists as soon as Claude writes it: wait for
        # the fetch (cat, then the rename of .part) to finish too, or the test races the background run.
        fetched = ["exec", "app-2", "--", "cat", str(self.result)]
        part = Path(str(self.result) + ".part")
        for _ in range(100):
            if self.result.exists() and fetched in self.f.calls("sbx") and not part.exists():
                return self.result.read_text()
            time.sleep(0.1)
        self.fail("the result was never fetched")

    def test_runs_claude_in_the_work_directory_and_fetches_the_result_without_a_workspace(self):
        out = self.launch(FAKE_CLAUDE_WRITES=str(self.result))

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "app-2")
        self.assertEqual(self.wait_for_result(), "review result")
        self.assertEqual(self.f.herdr_calls("workspace"), [])
        self.assertEqual(self.f.herdr_calls("pane"), [])
        [claude] = self.f.calls("claude")
        self.assertEqual(claude, ["-p", str(self.workdir), f"review in {self.workdir}"])
        self.assertIn(["exec", "app-2", "--", "cat", str(self.result)], self.f.calls("sbx"))
        self.assertIn("claude done", self.log.read_text())
        leftovers = [p for p in Path(tempfile.gettempdir()).glob("herdr-sbx-prompt-*")
                     if str(self.workdir) in p.read_text()]
        self.assertEqual(leftovers, [])

    def test_a_run_without_a_result_leaves_a_note_pointing_at_the_log(self):
        self.result.parent.mkdir()
        self.result.write_text("an earlier run's result")

        out = self.launch()

        self.assertEqual(out.returncode, 0, out.stderr)
        note = self.wait_for_result()
        self.assertIn("app-2", note)
        self.assertIn(str(self.log), note)


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
