import io
import json
import os
import unittest

from skyguard.commands_check import DESCRIPTIONS, commands_registered, main

with open(DESCRIPTIONS, encoding="utf-8") as _handle:
    OURS = json.load(_handle)

# What FPP 9.5.3 answered to /api/commands for SkyGuard's Test Trigger, on a real box.
REAL_95_TEST_TRIGGER = {"args": [
    {"default": "10", "description": "Seconds", "max": 3600, "min": 1, "name": "seconds", "optional": True, "type": "int"},
    {"default": "", "description": "Fixture ID (empty for all heads)", "name": "fixture", "optional": True, "type": "string"}],
    "name": "SkyGuard Test Trigger"}


def registered():
    return [{"name": c["name"], "args": [dict(a) for a in c.get("args", [])]} for c in OURS]


class CommandsCheckTests(unittest.TestCase):
    def test_the_descriptions_file_is_found(self):
        self.assertTrue(os.path.isfile(DESCRIPTIONS))
        self.assertTrue(OURS)

    def test_all_registered_as_described(self):
        self.assertTrue(commands_registered(OURS, registered() + [{"name": "Other Plugin", "args": []}]))

    def test_the_real_95_answer_matches_the_test_trigger_description(self):
        mine = next(c for c in OURS if c["name"] == "SkyGuard Test Trigger")
        self.assertTrue(commands_registered([mine], [REAL_95_TEST_TRIGGER]))

    def test_extra_fields_from_fpp_are_fine(self):
        doc = registered()
        for command in doc:
            command["extra"] = 1
            for arg in command["args"]:
                arg["contentListUrl"] = ""
        self.assertTrue(commands_registered(OURS, doc))

    def test_a_missing_command(self):
        self.assertFalse(commands_registered(OURS, registered()[1:]))

    def test_a_changed_argument(self):
        doc = registered()
        next(c for c in doc if c["args"])["args"][0]["default"] = "99"
        self.assertFalse(commands_registered(OURS, doc))

    def test_an_added_or_removed_argument(self):
        doc = registered()
        next(c for c in doc if c["args"])["args"].pop()
        self.assertFalse(commands_registered(OURS, doc))
        doc = registered()
        doc[0]["args"].append({"name": "new"})
        self.assertFalse(commands_registered(OURS, doc))

    def test_unreadable_answers_ask_for_the_restart(self):
        for text in ("", "not json", "{}", "null", "[1, 2]", "<html></html>"):
            self.assertEqual(main(io.StringIO(text)), 1, text)

    def test_a_good_answer_needs_no_restart(self):
        self.assertEqual(main(io.StringIO(json.dumps(registered()))), 0)


if __name__ == "__main__":
    unittest.main()
