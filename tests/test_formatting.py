from ai_control.bot.formatting import markdown_to_html, split_markdown


def test_markdown_to_html_renders_supported_subset_and_escapes() -> None:
    text = (
        "## Итог\n"
        "**упал с кодом 3** в `solve` <tag>\n"
        "- пункт & [ссылка](https://example.com/a?b=1)\n\n"
        "```\nerror <rc_1350> **raw**\n```"
    )

    rendered = markdown_to_html(text)

    assert "<b>Итог</b>" in rendered
    assert "<b>упал с кодом 3</b> в <code>solve</code> &lt;tag&gt;" in rendered
    assert '• пункт &amp; <a href="https://example.com/a?b=1">ссылка</a>' in rendered
    assert "<pre>error &lt;rc_1350&gt; **raw**</pre>" in rendered


def test_split_markdown_keeps_everything_and_respects_limit() -> None:
    paragraphs = [f"Абзац {index}: " + "слово " * 40 for index in range(30)]
    code = "```\n" + "\n".join(f"line {index}" for index in range(200)) + "\n```"
    text = "\n\n".join(paragraphs) + "\n\n" + code

    chunks = split_markdown(text, limit=1000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
    joined = "\n".join(chunks)
    for index in range(30):
        assert f"Абзац {index}:" in joined
    for index in range(200):
        assert f"line {index}\n" in joined or f"line {index}</pre>" in joined
    assert all(chunk.count("<pre>") == chunk.count("</pre>") for chunk in chunks)


def test_split_markdown_hard_splits_single_huge_line() -> None:
    chunks = split_markdown("&" * 5000, limit=500)

    assert all(len(chunk) <= 500 for chunk in chunks)
    assert sum(chunk.count("&amp;") for chunk in chunks) == 5000
