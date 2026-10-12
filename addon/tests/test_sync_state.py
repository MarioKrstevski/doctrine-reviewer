import tempfile, unittest
import sync_state as st

class StateTest(unittest.TestCase):
    def test_missing_is_unknown_release(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual({"release": "", "last_check": 0.0}, st.load(d))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            st.save(d, "1.1", 123.0)
            self.assertEqual({"release": "1.1", "last_check": 123.0}, st.load(d))

    def test_due_respects_interval_and_force(self):
        s = {"release": "1.0", "last_check": 1000.0}
        self.assertFalse(st.due(s, 1000 + st.CHECK_INTERVAL - 1))
        self.assertTrue(st.due(s, 1000 + st.CHECK_INTERVAL))
        self.assertTrue(st.due(s, 1001, force=True))
