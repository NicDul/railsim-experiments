import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from railsim_mcp.configuration import Settings, project_root, state_root
from railsim_mcp.workspace import ScenarioWorkspace, WorkspaceError


class ConfigurationTest(unittest.TestCase):
    def test_requires_explicit_simulator(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'RAILSIM_PROJECT_ROOT'):
                project_root()

    def test_roots_are_explicit_and_state_is_external(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'simulator'
            for name in ('mvnw', 'pom.xml', 'src/main/java/org/matsim/project/RunSingleSimulation.java'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / 'src/main/resources/scenarios').mkdir(parents=True)
            settings = Settings.load(str(root), str(Path(directory) / 'state'))
            self.assertEqual(settings.project, root.resolve())
            with self.assertRaisesRegex(ValueError, 'outside'):
                Settings.load(str(root), str(root / 'results'))

    def test_missing_runner_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Incompatible'):
                Settings.load(directory)

    def test_workspace_never_infers_engine_from_template_path(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = ScenarioWorkspace(Path(directory) / 'templates', Path(directory) / 'workspaces')
            self.assertIsNone(workspace.project_root)
            with self.assertRaisesRegex(WorkspaceError, 'No simulator'):
                workspace.run_once('test', 4711, 100)

    def test_state_path_environment_override(self):
        with patch.dict(os.environ, {'RAILSIM_STATE_ROOT': '/tmp/railsim-explicit-state'}):
            self.assertEqual(state_root(), Path('/tmp/railsim-explicit-state').resolve())
