import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
INSTALLER = ROOT_DIR / "scripts" / "install.sh"


class InstallerTests(unittest.TestCase):
    def run_git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit(self, repository: Path, message: str) -> str:
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Flawless Test",
                "-c",
                "user.email=flawless-test@example.invalid",
                "commit",
                "-am",
                message,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return self.run_git(repository, "rev-parse", "HEAD")

    def test_installer_clones_updates_and_protects_local_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            install = root / "install"
            source.mkdir()
            subprocess.run(
                ["git", "-C", str(source), "init", "--initial-branch=main"],
                check=True,
                capture_output=True,
                text=True,
            )
            readme = source / "README.md"
            readme.write_text("revision one\n", encoding="utf-8")
            self.run_git(source, "add", "README.md")
            first_revision = self.commit(source, "Initial fixture")

            environment = {
                **os.environ,
                "FLAWLESS_REPOSITORY_URL": str(source.resolve()),
                "FLAWLESS_INSTALL_DIR": str(install.resolve()),
                "FLAWLESS_BRANCH": "main",
            }
            first_install = subprocess.run(
                [str(INSTALLER), "--no-start"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(first_revision[:12], first_install.stdout)
            self.assertEqual(self.run_git(install, "rev-parse", "HEAD"), first_revision)

            readme.write_text("revision two\n", encoding="utf-8")
            second_revision = self.commit(source, "Update fixture")
            second_install = subprocess.run(
                [str(INSTALLER), "--no-start"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(second_revision[:12], second_install.stdout)
            self.assertEqual(self.run_git(install, "rev-parse", "HEAD"), second_revision)

            (install / "README.md").write_text("local change\n", encoding="utf-8")
            protected = subprocess.run(
                [str(INSTALLER), "--no-start"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(protected.returncode, 0)
            self.assertIn("local changes detected", protected.stderr)


if __name__ == "__main__":
    unittest.main()
