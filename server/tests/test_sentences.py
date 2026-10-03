from wristcall.sentences import SentenceSplitter


def test_two_sentences_and_remainder():
    s = SentenceSplitter()
    assert s.push("Hello. How are you? Fine") == ["Hello.", "How are you?"]
    assert s.flush() == ["Fine"]


def test_streaming_char_by_char():
    s = SentenceSplitter()
    out = []
    for ch in "Hi! How are you? ":
        out += s.push(ch)
    assert out == ["Hi!", "How are you?"]


def test_decimal_and_time_do_not_split():
    s = SentenceSplitter()
    assert s.push("The price is 3.5 dollars at 10:30 today") == []
    assert s.flush() == ["The price is 3.5 dollars at 10:30 today"]


def test_repeated_punctuation_quotes_and_ellipsis():
    s = SentenceSplitter()
    assert s.push('Hello!! He said "stop." Hmm… right. ') == ["Hello!!", 'He said "stop."', "Hmm…", "right."]


def test_long_text_without_punctuation_is_cut_at_spaces():
    s = SentenceSplitter(max_chars=200)
    text = "message " * 60
    pieces = s.push(text) + s.flush()
    assert len(pieces) == 3
    assert all(len(p) <= 200 for p in pieces)
    assert " ".join(pieces).split() == text.split()


def test_flush_empty():
    s = SentenceSplitter()
    assert s.flush() == []
    s.push("   ")
    assert s.flush() == []
