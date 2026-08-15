"""Unit tests for FPSCalculator class."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from annotation import FPSCalculator


class TestFPSCalculator:
    """Tests for FPSCalculator."""

    def test_returns_zero_with_no_timestamps(self):
        """FPS should be 0.0 when no timestamps recorded."""
        calc = FPSCalculator()
        assert calc.get_fps() == 0.0

    def test_returns_zero_with_one_timestamp(self):
        """FPS should be 0.0 when only one timestamp recorded."""
        calc = FPSCalculator()
        calc.tick(1.0)
        assert calc.get_fps() == 0.0

    def test_two_timestamps_one_second_apart(self):
        """Two timestamps 1s apart should give FPS of 1.0."""
        calc = FPSCalculator()
        calc.tick(1.0)
        calc.tick(2.0)
        # (2-1) / (2.0 - 1.0) = 1.0
        assert calc.get_fps() == 1.0

    def test_ten_frames_per_second(self):
        """10 timestamps over 1 second should give ~9.0 FPS."""
        calc = FPSCalculator()
        for i in range(10):
            calc.tick(1.0 + i * 0.1)
        # (10-1) / (1.9 - 1.0) = 9 / 0.9 = 10.0
        assert calc.get_fps() == 10.0

    def test_rolling_window_removes_old_timestamps(self):
        """Timestamps older than 2 seconds should be removed."""
        calc = FPSCalculator(window_seconds=2.0)
        # Add timestamps at t=0, t=1, t=2, t=3
        calc.tick(0.0)
        calc.tick(1.0)
        calc.tick(2.0)
        calc.tick(3.0)
        # After tick(3.0), cutoff is 1.0, so t=0.0 is removed
        # Remaining: [1.0, 2.0, 3.0]
        # FPS = (3-1) / (3.0 - 1.0) = 2/2 = 1.0
        assert calc.get_fps() == 1.0

    def test_format_fps_one_decimal(self):
        """FPS should be rounded to 1 decimal place."""
        calc = FPSCalculator()
        # 3 timestamps: 0.0, 0.3, 0.7
        calc.tick(0.0)
        calc.tick(0.3)
        calc.tick(0.7)
        # (3-1) / (0.7 - 0.0) = 2 / 0.7 = 2.857...
        # Rounded to 1 decimal = 2.9
        assert calc.get_fps() == 2.9

    def test_identical_timestamps_returns_zero(self):
        """If all timestamps are identical, duration is 0, return 0.0."""
        calc = FPSCalculator()
        calc.tick(5.0)
        calc.tick(5.0)
        calc.tick(5.0)
        assert calc.get_fps() == 0.0

    def test_tick_without_argument_uses_current_time(self):
        """tick() without argument should use time.time()."""
        calc = FPSCalculator()
        calc.tick()
        # Should have one timestamp, FPS = 0.0
        assert calc.get_fps() == 0.0

    def test_custom_window_size(self):
        """Custom window size should be respected."""
        calc = FPSCalculator(window_seconds=1.0)
        calc.tick(0.0)
        calc.tick(0.5)
        calc.tick(1.0)
        calc.tick(1.5)
        # After tick(1.5), cutoff is 0.5, so t=0.0 is removed
        # Remaining: [0.5, 1.0, 1.5]
        # FPS = (3-1) / (1.5 - 0.5) = 2/1.0 = 2.0
        assert calc.get_fps() == 2.0
