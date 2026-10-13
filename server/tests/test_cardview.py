import unittest

import cardview as cv


class SanitizeTest(unittest.TestCase):
    def test_scripts_handlers_and_js_urls_are_removed(self):
        out = cv.sanitize('<b onclick="x()">hi</b><script>alert(1)</script><a href="javascript:alert(1)">l</a>'
                          '<img src="pic.png" onerror="x()"><iframe src="e"></iframe>')
        self.assertNotIn("script", out)
        self.assertNotIn("onclick", out)
        self.assertNotIn("onerror", out)
        self.assertNotIn("javascript", out)
        self.assertNotIn("iframe", out)
        self.assertIn('<img src="/media/pic.png" loading="lazy">', out)
        self.assertIn("<b>hi</b>", out)

    def test_absolute_urls_and_entities_survive(self):
        out = cv.sanitize('<img src="https://x/y.png"> &gt;95% a &amp; b')
        self.assertIn('src="https://x/y.png"', out)
        self.assertIn("&gt;95%", out)
        self.assertIn("&amp;", out)


class ClozeTest(unittest.TestCase):
    def test_front_hides_and_back_reveals_only_the_active_cloze(self):
        t = "A {{c1::one::hint}} and {{c2::two}}"
        f = cv.faces({"Text": t, "Extra": "<b>e</b>"}, 1)
        self.assertIn("[hint]", f["front"])
        self.assertIn("two", f["front"])
        self.assertNotIn("one", f["front"])
        self.assertIn('class="cloze-show">one', f["back"])
        self.assertEqual("<b>e</b>", f["extra"])
        self.assertIn("[...]", cv.faces({"Text": t}, 2)["front"])

    def test_numbers_and_basic_notes(self):
        self.assertEqual([1, 3], cv.cloze_numbers("{{c3::x}} {{c1::y}}"))
        f = cv.faces({"Front": "Q", "Back": "A"})
        self.assertEqual(("Q", "A"), (f["front"], f["back"]))


if __name__ == "__main__":
    unittest.main()
