"""Lọc chú thích ngoặc ở lớp hiển thị; giữ nguyên thuộc tính dữ liệu và biểu mẫu."""
from html.parser import HTMLParser
import re

_PARENS = re.compile(r'\s*\([^()]*\)')
_DESCRIPTION_ATTR = re.compile(r'''(?P<key>\b(?:placeholder|title|aria-label|alt)\s*=\s*)(?P<q>["'])(?P<text>.*?)(?P=q)''', re.I | re.S)


def display_text(text):
    # Lặp để loại cả ngoặc lồng nhau; không nuốt đoạn sau một dấu ngoặc chưa đóng.
    while True:
        result = _PARENS.sub('', text)
        if result == text:
            return result
        text = result


class _DisplayHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.protected = 0

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        if not self.protected:
            raw = _DESCRIPTION_ATTR.sub(lambda m: m['key'] + m['q'] + display_text(m['text']) + m['q'], raw)
        self.parts.append(raw)
        if tag in ('script', 'style', 'textarea'):
            self.protected += 1

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append('</' + tag + '>')
        if tag in ('script', 'style', 'textarea'):
            self.protected = max(0, self.protected - 1)

    def handle_data(self, data):
        self.parts.append(data if self.protected else display_text(data))

    def handle_entityref(self, name):
        self.parts.append('&' + name + ';')

    def handle_charref(self, name):
        self.parts.append('&#' + name + ';')

    def handle_comment(self, data):
        self.parts.append('<!--' + data + '-->')

    def handle_decl(self, decl):
        self.parts.append('<!' + decl + '>')


def display_html(html):
    parser = _DisplayHTML()
    parser.feed(html)
    parser.close()
    return ''.join(parser.parts)
