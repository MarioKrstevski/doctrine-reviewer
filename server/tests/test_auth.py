from datetime import datetime, timedelta, timezone
import unittest

import auth


class HashPasswordTest(unittest.TestCase):
    def test_a_hash_verifies_against_its_own_password(self):
        stored = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", stored))

    def test_a_wrong_password_does_not_verify(self):
        stored = auth.hash_password("right")
        self.assertFalse(auth.verify_password("wrong", stored))

    def test_the_same_password_hashes_differently_each_time(self):
        a = auth.hash_password("same")
        b = auth.hash_password("same")
        self.assertNotEqual(a, b, "missing per-user salt")
        self.assertTrue(auth.verify_password("same", a))
        self.assertTrue(auth.verify_password("same", b))

    def test_verify_never_raises_on_a_malformed_stored_value(self):
        for junk in ["", "garbage", "scrypt$onlyonefield", "scrypt$zz$zz", None]:
            with self.subTest(junk=junk):
                self.assertFalse(auth.verify_password("x", junk))

    def test_an_empty_password_is_still_handled(self):
        stored = auth.hash_password("")
        self.assertTrue(auth.verify_password("", stored))
        self.assertFalse(auth.verify_password("x", stored))

    def test_unicode_passwords_round_trip(self):
        stored = auth.hash_password("café—日本語")
        self.assertTrue(auth.verify_password("café—日本語", stored))


class TokenTest(unittest.TestCase):
    def test_tokens_are_long_and_unique(self):
        tokens = {auth.new_token() for _ in range(200)}
        self.assertEqual(200, len(tokens), "tokens collided")
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_generated_passwords_are_unique_and_reasonable(self):
        pws = {auth.generate_password() for _ in range(100)}
        self.assertEqual(100, len(pws))
        self.assertTrue(all(len(p) >= 12 for p in pws))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    def test_a_fresh_session_is_not_expired(self):
        expiry = auth.session_expiry(self.now)
        self.assertFalse(auth.is_expired(expiry, self.now))

    def test_a_session_expires_after_the_window(self):
        expiry = auth.session_expiry(self.now)
        later = self.now + timedelta(days=auth.SESSION_DAYS, seconds=1)
        self.assertTrue(auth.is_expired(expiry, later))

    def test_the_boundary_counts_as_expired(self):
        expiry = auth.session_expiry(self.now)
        exactly = self.now + timedelta(days=auth.SESSION_DAYS)
        self.assertTrue(auth.is_expired(expiry, exactly))

    def test_an_unparseable_expiry_is_treated_as_expired(self):
        for junk in [None, "", "not-a-date", 12345]:
            with self.subTest(junk=junk):
                self.assertTrue(auth.is_expired(junk, self.now))


class ThrottleTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    def _recent(self, count):
        return [(self.now - timedelta(minutes=1)).isoformat()] * count

    def test_below_the_limit_is_allowed(self):
        limit = auth.THROTTLE_MAX_ATTEMPTS
        self.assertFalse(auth.too_many_attempts(self._recent(limit - 1), self.now))

    def test_at_the_limit_is_blocked(self):
        limit = auth.THROTTLE_MAX_ATTEMPTS
        self.assertTrue(auth.too_many_attempts(self._recent(limit), self.now))

    def test_old_attempts_do_not_count(self):
        old = (self.now - timedelta(minutes=auth.THROTTLE_WINDOW_MINUTES + 1)).isoformat()
        stale = [old] * (auth.THROTTLE_MAX_ATTEMPTS * 3)
        self.assertFalse(auth.too_many_attempts(stale, self.now))

    def test_junk_timestamps_are_ignored_rather_than_crashing(self):
        entries = self._recent(auth.THROTTLE_MAX_ATTEMPTS - 1) + [None, "nope"]
        self.assertFalse(auth.too_many_attempts(entries, self.now))


class CsrfTest(unittest.TestCase):
    def test_a_token_matches_itself(self):
        t = auth.csrf_token("session-abc", "secret")
        self.assertTrue(auth.csrf_ok(t, "session-abc", "secret"))

    def test_a_token_from_another_session_does_not_match(self):
        t = auth.csrf_token("session-abc", "secret")
        self.assertFalse(auth.csrf_ok(t, "session-xyz", "secret"))

    def test_missing_or_junk_tokens_are_rejected(self):
        for junk in [None, "", "deadbeef", 42]:
            with self.subTest(junk=junk):
                self.assertFalse(auth.csrf_ok(junk, "session-abc", "secret"))
