"""Passive-instance flag on /api/config — the TEST badge's server side.

Run: .venv/bin/python -m unittest tests.test_passive_badge
"""
import os
import unittest
from unittest import mock

from server import main


class PassiveFlagTests(unittest.TestCase):
    def test_passive_set(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            self.assertTrue(main.api_config()["passive"])

    def test_passive_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "VIRA_PASSIVE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(main.api_config()["passive"])
