"""A rough PHP syntax check, for machines without PHP. It catches the mistakes that break a
whole page: an unescaped quote ending a string early, and unbalanced brackets. Run
`php -l` on the Pi as well; this is not a parser."""

import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = {")": "(", "]": "[", "}": "{"}


def php_problems(text):
    """Problems found in the PHP parts of a file, as (line, message)."""
    problems = []
    i, line, n = 0, 1, len(text)
    in_php = False
    stack = []

    def advance(count):
        nonlocal i, line
        line += text.count("\n", i, i + count)
        i += count

    while i < n:
        if not in_php:
            start = text.find("<?php", i)
            short = text.find("<?=", i)
            starts = [s for s in (start, short) if s >= 0]
            if not starts:
                break
            nxt = min(starts)
            advance(nxt - i + (5 if nxt == start else 3))
            in_php = True
            block_line = line
            continue
        ch = text[i]
        if text.startswith("?>", i):
            if stack:
                problems.append((block_line, f"unclosed '{stack[-1]}' in a PHP block"))
                stack = []
            in_php = False
            advance(2)
        elif ch in "'\"":
            quote, open_line = ch, line
            advance(1)
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    advance(1)
                advance(1)
            if i >= n:
                problems.append((open_line, f"string opened with {quote} never closes"))
                break
            advance(1)
        elif text.startswith("//", i) or ch == "#":
            end = text.find("\n", i)
            close = text.find("?>", i)
            stop = min(x for x in (end, close, n) if x >= 0)
            advance(stop - i)
        elif text.startswith("/*", i):
            end = text.find("*/", i)
            advance((end + 2 if end >= 0 else n) - i)
        elif text.startswith("<<<", i):
            label_end = text.find("\n", i)
            label = text[i + 3:label_end].strip().strip("'\"")
            end = text.find("\n" + label, label_end)
            if end < 0:
                problems.append((line, "heredoc never closes"))
                break
            advance(end + 1 + len(label) - i)
        elif ch in "([{":
            stack.append(ch)
            advance(1)
        elif ch in ")]}":
            if not stack or stack[-1] != PAIRS[ch]:
                problems.append((line, f"unexpected '{ch}'"))
            else:
                stack.pop()
            advance(1)
        else:
            advance(1)
    if in_php and stack:
        problems.append((line, f"unclosed '{stack[-1]}' at end of file"))
    return problems


def strings_crossing_tags(text):
    """Lines where a short `<?php ... ?>` holds an odd number of unescaped single quotes,
    which means a quote inside a string ended it early."""
    found = []
    for number, row in enumerate(text.splitlines(), 1):
        start = row.find("<?php")
        end = row.find("?>", start + 5)
        if start < 0 or end < 0:
            continue
        code = row[start + 5:end]
        quotes, k, in_double = 0, 0, False
        while k < len(code):
            c = code[k]
            if c == "\\":
                k += 2
                continue
            if c == '"' and quotes % 2 == 0:
                in_double = not in_double
            elif c == "'" and not in_double:
                quotes += 1
            k += 1
        if quotes % 2:
            found.append(number)
    return found


def php_files():
    return sorted(glob.glob(os.path.join(ROOT, "*.php")))


class PhpSyntaxTests(unittest.TestCase):
    def test_php_files_have_balanced_strings_and_brackets(self):
        self.assertTrue(php_files())
        for path in php_files():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            with self.subTest(file=os.path.basename(path)):
                self.assertEqual(php_problems(text), [])
                self.assertEqual(strings_crossing_tags(text), [])

    def test_the_check_catches_a_stray_apostrophe(self):
        broken = "<p>\n<?php skyGuardRowOpen('Show elevation', '', 'Your show's height.'); ?>\n<p>\n"
        self.assertEqual(strings_crossing_tags(broken), [2])
        self.assertEqual(strings_crossing_tags(broken.replace("show's", "show\\'s")), [])

    def test_the_check_catches_an_unclosed_bracket(self):
        self.assertTrue(php_problems("<?php\nfunction f() {\n    return array(1, 2;\n}\n"))
        self.assertEqual(php_problems("<?php\nfunction f() {\n    return array(1, 2);\n}\n"), [])


if __name__ == "__main__":
    unittest.main()
