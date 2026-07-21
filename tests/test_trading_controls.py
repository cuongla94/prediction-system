"""Unit tests for trading controls (master toggle)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from risk.controls import is_real_money_enabled, set_real_money_enabled


class TestIsRealMoneyEnabled:
    def test_database_unavailable_fails_closed(self):
        """If database is unavailable, returns False (fail-closed)."""
        with patch("risk.controls.get_db", return_value=None):
            assert is_real_money_enabled() is False

    def test_no_rows_returns_false(self):
        """If no rows exist in table, returns False."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            assert is_real_money_enabled() is False

    def test_single_row_enabled_true(self):
        """Single row with enabled=True returns True."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [(True,)]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            assert is_real_money_enabled() is True

    def test_single_row_enabled_false(self):
        """Single row with enabled=False returns False."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [(False,)]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            assert is_real_money_enabled() is False

    def test_multiple_rows_fails_closed(self):
        """Multiple rows (data integrity error) fails closed, returns False."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [(True,), (False,)]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            assert is_real_money_enabled() is False

    def test_query_exception_fails_closed(self):
        """If query raises an exception, fails closed, returns False."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("connection lost")
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            assert is_real_money_enabled() is False


class TestSetRealMoneyEnabled:
    def test_database_unavailable_returns_false(self):
        """If database is unavailable, returns False."""
        with patch("risk.controls.get_db", return_value=None):
            result = set_real_money_enabled(True, updated_by="test")
            assert result is False

    def test_successful_insert_returns_true(self):
        """Successful insert returns True."""
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            result = set_real_money_enabled(True, updated_by="test_user", note="edge cleared")
            assert result is True
            # Verify the insert was called
            mock_cursor.execute.assert_called_once()
            mock_db.commit.assert_called_once()

    def test_insert_exception_returns_false(self):
        """If insert raises an exception, returns False."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("insert failed")
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor

        with patch("risk.controls.get_db", return_value=mock_db):
            result = set_real_money_enabled(True, updated_by="test")
            assert result is False
