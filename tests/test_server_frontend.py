import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class FrontendResolutionTests(unittest.TestCase):
    def test_source_mode_finds_frontend_beside_server_when_data_root_is_external(self) -> None:
        """Launcher data isolation must not make the source frontend disappear."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            frontend_dist = project_root / "frontend" / "dist"
            frontend_dist.mkdir(parents=True)
            (frontend_dist / "index.html").write_text("demo", encoding="utf-8")
            external_data_root = temp_root / "app-data"

            with (
                patch.object(server, "ROOT", external_data_root),
                patch.object(server, "__file__", str(project_root / "server.py")),
                patch.object(sys, "frozen", False, create=True),
            ):
                self.assertEqual(server._resolve_frontend_dist(), frontend_dist)


if __name__ == "__main__":
    unittest.main()
