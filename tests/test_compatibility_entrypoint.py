import unittest


class CompatibilityEntrypointTests(unittest.TestCase):
    def test_legacy_frontend_server_imports_control_plane_app(self):
        from backend.app.main import app as control_plane_app
        from frontend.server import app as compatibility_app

        self.assertIs(compatibility_app, control_plane_app)


if __name__ == "__main__":
    unittest.main()
