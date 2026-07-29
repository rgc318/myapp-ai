import subprocess
import sys
from unittest import TestCase


class TestRuntimeFixtureIsolation(TestCase):
	def test_production_application_import_does_not_load_evaluation_replay_modules(self):
		result = subprocess.run(
			[
				sys.executable,
				"-c",
				(
					"import sys; import myapp_ai.main; "
					"loaded = sorted(name for name in sys.modules if name.startswith('myapp_ai.evals')); "
					"assert not loaded, loaded"
				),
			],
			capture_output=True,
			check=False,
			text=True,
		)

		self.assertEqual(result.returncode, 0, result.stderr)
