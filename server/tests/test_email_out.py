"""Nothing sends mail. Turning sending on without a way to send must fail
loudly at boot, never quietly drop messages."""

import unittest

import config
import email_out


class ConfigTest(unittest.TestCase):
    def test_sending_is_off_by_default(self):
        self.assertFalse(config.load({}).send_email)
        self.assertEqual("", config.load({}).smtp_url)

    def test_truthy_values_turn_it_on(self):
        for v in ("1", "true", "TRUE", "yes"):
            with self.subTest(v=v):
                self.assertTrue(config.load({"SEND_EMAIL": v}).send_email)

    def test_falsy_values_keep_it_off(self):
        for v in ("0", "false", "", "no"):
            with self.subTest(v=v):
                self.assertFalse(config.load({"SEND_EMAIL": v}).send_email)


class BootGuardTest(unittest.TestCase):
    def test_off_needs_nothing(self):
        email_out.check_config(config.load({}))

    def test_on_without_smtp_url_refuses_to_boot(self):
        with self.assertRaises(RuntimeError) as ctx:
            email_out.check_config(config.load({"SEND_EMAIL": "true"}))
        self.assertIn("SMTP_URL", str(ctx.exception))

    def test_on_with_smtp_url_passes_the_guard(self):
        email_out.check_config(config.load({"SEND_EMAIL": "true",
                                            "SMTP_URL": "smtp://user:pw@host:587"}))


class SendTest(unittest.TestCase):
    def test_off_is_a_noop_that_reports_not_sent(self):
        sent = email_out.send({"email": "a@b.c", "subject": "s", "body": "b"},
                              config.load({}))
        self.assertFalse(sent)

    def test_on_is_not_implemented_yet_rather_than_pretending(self):
        cfg = config.load({"SEND_EMAIL": "true", "SMTP_URL": "smtp://h"})
        with self.assertRaises(NotImplementedError):
            email_out.send({"email": "a@b.c", "subject": "s", "body": "b"}, cfg)
