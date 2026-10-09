import base64

import pytest

from wristcall.history_codec import HistoryCodec, HistoryKeyError, new_key, parse_key, words

KEY = bytes(range(32))


def test_words_drop_accents_case_and_punctuation():
    assert words("Reunião às 15h, com o João! São-Paulo_SP") == ["reuniao", "as", "15h", "com", "o", "joao", "sao", "paulo", "sp"]
    assert words("ÉCOLE Straße") == ["ecole", "strasse"]
    assert words("“...” — !!") == []
    assert words("a" * 100) == ["a" * 64]


def test_keys_parse_from_both_base64_alphabets():
    assert parse_key(base64.b64encode(KEY).decode()) == KEY
    assert parse_key(base64.urlsafe_b64encode(KEY).decode().rstrip("=")) == KEY
    assert len(parse_key(new_key())) == 32
    assert new_key() != new_key()


@pytest.mark.parametrize("bad", ["", "abc", base64.b64encode(b"x" * 31).decode(), "not base64 at all!!", "é" * 43])
def test_bad_keys_are_refused(bad):
    with pytest.raises(HistoryKeyError, match="32 random bytes"):
        parse_key(bad)


def test_plain_codec_stores_text_and_words():
    codec = HistoryCodec()
    assert not codec.encrypted and codec.key_id is None
    assert codec.seal("Olá mundo", "c_1:0") == ("Olá mundo", False)
    assert codec.open("Olá mundo", False, "c_1:0") == "Olá mundo"
    assert codec.index_terms("Olá olá MUNDO") == ["ola", "mundo"]
    assert codec.query_terms("mundo Olá mundo") == [["mundo"], ["ola"]]


def test_sealed_text_round_trips_and_hides_the_words():
    codec = HistoryCodec(KEY)
    stored, sealed = codec.seal("comprar leite amanhã", "c_1:0")
    assert sealed and "leite" not in stored
    assert codec.seal("comprar leite amanhã", "c_1:0")[0] != stored  # fresh nonce every time
    assert codec.open(stored, True, "c_1:0") == "comprar leite amanhã"
    terms = codec.index_terms("comprar leite amanhã")
    assert len(terms) == 3 and all(t.startswith("x") and len(t) == 17 for t in terms)
    assert "leite" not in " ".join(terms)
    assert codec.index_terms("LEITE") == [terms[1]]  # same word, same term
    assert codec.query_terms("Leite") == [["leite", terms[1]]]  # plain word too: entries from before the key


def test_sealed_text_is_bound_to_its_row():
    codec = HistoryCodec(KEY)
    stored, _ = codec.seal("segredo", "c_1:0")
    with pytest.raises(HistoryKeyError):
        codec.open(stored, True, "c_2:0")


def test_another_key_or_no_key_cannot_open():
    stored, _ = HistoryCodec(KEY).seal("segredo", "c_1:0")
    with pytest.raises(HistoryKeyError, match="another key"):
        HistoryCodec(bytes(32)).open(stored, True, "c_1:0")
    with pytest.raises(HistoryKeyError, match="set history.encryption_key"):
        HistoryCodec().open(stored, True, "c_1:0")
    with pytest.raises(HistoryKeyError):
        HistoryCodec(KEY).open("garbage!", True, "c_1:0")


def test_key_id_names_the_key_without_revealing_it():
    a, b = HistoryCodec(KEY), HistoryCodec(bytes(32))
    assert a.key_id == HistoryCodec(KEY).key_id and a.key_id != b.key_id
    assert len(a.key_id) == 16 and KEY.hex()[:16] != a.key_id
    assert a.index_terms("leite") != b.index_terms("leite")


def test_queries_are_capped_at_sixteen_words():
    query = " ".join(f"w{i}" for i in range(30))
    assert len(HistoryCodec().query_terms(query)) == 16
    assert HistoryCodec().query_terms("!!! ...") == []
