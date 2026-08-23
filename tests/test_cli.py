"""CLI entry points.

These exist because a settings refactor broke --selftest while every unit
test still passed: nothing exercised main() itself. Cheap smoke coverage of
each mode of operation catches that class of regression.
"""

from __future__ import annotations

import pytest

from app.main import _parse_args, main


class TestArgs:
    def test_defaults(self):
        args = _parse_args([])
        assert not args.selftest and not args.headless and not args.diagnose
        assert args.mode is None

    def test_mode_accepts_both_games(self):
        assert _parse_args(["--mode", "f1_25"]).mode == "f1_25"
        assert _parse_args(["--mode", "f1_26"]).mode == "f1_26"

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(SystemExit):
            _parse_args(["--mode", "f1_99"])


class TestSelftest:
    @pytest.mark.parametrize("mode", ["f1_25", "f1_26"])
    def test_selftest_succeeds_in_each_mode(self, mode, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        code = main(["--selftest", "--mode", mode, "--port", "20997"])
        out = capsys.readouterr().out

        assert code == 0, out
        assert "All subsystems started correctly" in out
        # The active mode must be reported, not silently assumed.
        assert ("F1 25" if mode == "f1_25" else "F1 26") in out

    def test_port_override_applies_to_the_active_mode(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("RHE_DATA_DIR", str(tmp_path))
        main(["--selftest", "--port", "20998"])
        assert "20998" in capsys.readouterr().out
