import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import run


class LauncherTests(unittest.TestCase):
    def test_health_check_uses_proxy_free_local_opener(self) -> None:
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"status":"ok","service":"tradewind"}'
        response.__enter__.return_value = response
        with patch.object(run._LOCAL_OPENER, "open", return_value=response) as open_local:
            self.assertTrue(run.is_tradewind_ready())
        open_local.assert_called_once_with(run.HEALTH_URL, timeout=0.5)

    def test_default_data_root_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"LOCALAPPDATA": temp_dir}, clear=False),
                patch.object(run, "executable_dir", return_value=Path(temp_dir) / "program"),
            ):
                os.environ.pop("TRADEWIND_DATA_DIR", None)
                self.assertEqual(run.resolve_data_root(), Path(temp_dir).resolve() / "Tradewind")

    def test_portable_flag_keeps_data_beside_program(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            program_dir = Path(temp_dir)
            (program_dir / "portable.flag").touch()
            with patch.object(run, "executable_dir", return_value=program_dir):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("TRADEWIND_DATA_DIR", None)
                    self.assertEqual(run.resolve_data_root(), program_dir)

    def test_existing_service_only_reopens_browser(self) -> None:
        with (
            patch.object(run, "is_tradewind_ready", return_value=True),
            patch.object(run.webbrowser, "open") as open_browser,
            patch.object(run, "configure_runtime_data") as configure_runtime,
        ):
            self.assertEqual(run.main(), 0)
            open_browser.assert_called_once_with(run.BROWSER_URL)
            configure_runtime.assert_not_called()

    def test_browser_can_be_suppressed_for_portable_smoke_test(self) -> None:
        with (
            patch.dict(os.environ, {"TRADEWIND_NO_BROWSER": "1"}, clear=False),
            patch.object(run.webbrowser, "open") as open_browser,
        ):
            run.open_browser()
            open_browser.assert_not_called()

    def test_listening_port_falls_back_to_direct_browser_open(self) -> None:
        with (
            patch.object(run, "wait_until_ready", return_value=False),
            patch.object(run, "port_is_in_use", return_value=True),
            patch.object(run, "open_browser") as open_browser,
        ):
            run.open_browser_when_ready()
        open_browser.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
