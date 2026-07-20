import os
import unittest

os.environ.setdefault(
	"MYAPP_AI_SERVICE_TOKEN",
	"0123456789abcdef0123456789abcdef",
)

suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
