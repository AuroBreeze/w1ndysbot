import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from modules.GroupBanWords.handlers.data_manager_words import (
    DEFAULT_AD_WORDS,
    DataManager,
)


class DefaultAdWordsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = DataManager._db_path
        self.original_conn = DataManager._conn
        self.original_initialized = DataManager._initialized
        DataManager._db_path = os.path.join(self.temp_dir.name, "global_data.db")
        DataManager._conn = None
        DataManager._initialized = False

    def tearDown(self):
        if DataManager._conn:
            DataManager._conn.close()
        DataManager._db_path = self.original_path
        DataManager._conn = self.original_conn
        DataManager._initialized = self.original_initialized
        self.temp_dir.cleanup()

    def _reopen(self):
        DataManager.close_global_connection()
        return DataManager("10001")

    def test_first_initialization_seeds_global_ad_rules(self):
        manager = DataManager("10001")

        with DataManager("0") as global_manager:
            words = dict(global_manager.get_all_words_and_weight())

        self.assertEqual(words, DEFAULT_AD_WORDS)
        weight, matched = manager.calc_message_weight("招聘刷单，立即报名")
        self.assertEqual(weight, 100)
        self.assertTrue(matched)

    def test_existing_rule_weight_is_not_overwritten(self):
        pattern = next(iter(DEFAULT_AD_WORDS))
        with closing(sqlite3.connect(DataManager._db_path)) as conn:
            with conn:
                conn.execute(
                    """CREATE TABLE ban_words(
                           group_id TEXT NOT NULL, word TEXT NOT NULL,
                           weight INTEGER NOT NULL, update_time TEXT,
                           PRIMARY KEY(group_id,word))"""
                )
                conn.execute(
                    "INSERT INTO ban_words VALUES(?,?,?,?)",
                    ("0", pattern, 35, "old"),
                )

        DataManager("10001")

        with DataManager("0") as global_manager:
            words = dict(global_manager.get_all_words_and_weight())
        self.assertEqual(words[pattern], 35)

    def test_deleted_default_rule_is_not_restored_on_restart(self):
        pattern = next(iter(DEFAULT_AD_WORDS))
        DataManager("10001")
        with DataManager("0") as global_manager:
            self.assertTrue(global_manager.delete_word(pattern))

        self._reopen()

        with DataManager("0") as global_manager:
            words = dict(global_manager.get_all_words_and_weight())
        self.assertNotIn(pattern, words)


if __name__ == "__main__":
    unittest.main()
