import pytest

from receptionist.agent.speech import spell_codes, spell_confirmation_codes


async def stream(*chunks: str):
    for c in chunks:
        yield c


async def collect(*chunks: str) -> str:
    return "".join([c async for c in spell_confirmation_codes(stream(*chunks))])


def test_spell_codes():
    assert spell_codes("Your code is LKC-7Q2M.") == "Your code is L K C, 7 Q 2 M."
    assert spell_codes("No code here, Lakeside.") == "No code here, Lakeside."


@pytest.mark.parametrize(
    "chunks",
    [
        ("Your code is LKC-7Q2M, see you soon.",),
        ("Your code is L", "KC-7", "Q2M, see you soon."),
        ("Your code is LKC-", "7Q2", "M", ", see you soon."),
    ],
)
async def test_codes_split_across_chunks(chunks):
    assert await collect(*chunks) == "Your code is L K C, 7 Q 2 M, see you soon."


async def test_ordinary_words_starting_with_l_pass_through():
    assert await collect("Welcome to L", "akeside clinic") == "Welcome to Lakeside clinic"
