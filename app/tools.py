"""Small administration tools."""

from html.parser import HTMLParser


class HtmlToMarkdownParser(HTMLParser):
    """Convert common HTML structures to readable Markdown."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.link_stack = []
        self.list_stack = []
        self.in_pre = False

    def push(self, value):
        if value:
            self.parts.append(value)

    def ensure_blank_line(self):
        current = "".join(self.parts)
        if current and not current.endswith("\n\n"):
            self.push("\n\n" if not current.endswith("\n") else "\n")

    def handle_starttag(self, tag, attrs):
        attrs_map = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.ensure_blank_line()
            self.push(f"{'#' * int(tag[1])} ")
        elif tag in {"p", "section", "article", "div"}:
            self.ensure_blank_line()
        elif tag == "br":
            self.push("\n")
        elif tag in {"strong", "b"}:
            self.push("**")
        elif tag in {"em", "i"}:
            self.push("*")
        elif tag == "code" and not self.in_pre:
            self.push("`")
        elif tag == "pre":
            self.ensure_blank_line()
            self.push("```\n")
            self.in_pre = True
        elif tag == "a":
            self.push("[")
            self.link_stack.append(attrs_map.get("href", ""))
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self.ensure_blank_line()
        elif tag == "li":
            self.push("\n- ")
        elif tag == "blockquote":
            self.ensure_blank_line()
            self.push("> ")

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "section", "article", "div"}:
            self.ensure_blank_line()
        elif tag in {"strong", "b"}:
            self.push("**")
        elif tag in {"em", "i"}:
            self.push("*")
        elif tag == "code" and not self.in_pre:
            self.push("`")
        elif tag == "pre":
            self.push("\n```\n\n")
            self.in_pre = False
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            self.push(f"]({href})" if href else "]")
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.ensure_blank_line()

    def handle_data(self, data):
        if self.in_pre:
            self.push(data)
        else:
            text = " ".join(data.split())
            if not text:
                return
            if data[:1].isspace() and self.parts and not self.parts[-1].endswith((" ", "\n")):
                text = " " + text
            if data[-1:].isspace():
                text += " "
            self.push(text)

    def markdown(self):
        text = "".join(self.parts)
        lines = [line.rstrip() for line in text.splitlines()]
        compact_lines = []
        blank_seen = False
        for line in lines:
            is_blank = not line.strip()
            if is_blank and blank_seen:
                continue
            compact_lines.append(line)
            blank_seen = is_blank
        return "\n".join(compact_lines).strip() + ("\n" if compact_lines else "")


def html_to_markdown(html):
    """Convert HTML text to Markdown using only the standard library."""
    parser = HtmlToMarkdownParser()
    parser.feed(html or "")
    parser.close()
    return parser.markdown()
