import perspective_stub as stub

ATTRIBUTES = ["TOXICITY", "REASONING_EXPERIMENTAL", "INSULT"]


class TestAttributeScore:
    def test_score_is_in_the_probability_range(self):
        # The api reads these as Perspective summaryScore values, which are
        # probabilities; anything outside [0, 1] would skew the weighted sum.
        for attribute in ATTRIBUTES:
            score = stub.attribute_score("some post text", attribute)
            assert 0.0 <= score <= 1.0

    def test_same_text_and_attribute_score_identically(self):
        # A feed's order shouldn't change just because it was regenerated.
        first = stub.attribute_score("hello world", "TOXICITY")
        second = stub.attribute_score("hello world", "TOXICITY")
        assert first == second

    def test_different_attributes_of_one_post_differ(self):
        # Keyed on both text and attribute, so a post isn't uniformly "bad".
        scores = {a: stub.attribute_score("hello world", a) for a in ATTRIBUTES}
        assert len(set(scores.values())) == len(ATTRIBUTES)

    def test_different_texts_differ(self):
        assert stub.attribute_score("alpha", "TOXICITY") != stub.attribute_score("beta", "TOXICITY")

    def test_empty_text_is_handled(self):
        assert 0.0 <= stub.attribute_score("", "TOXICITY") <= 1.0

    def test_unicode_text_is_handled(self):
        # Real fixture content is full of non-ASCII; hashing must not raise.
        assert 0.0 <= stub.attribute_score("ぺぐふわ 😌💕", "TOXICITY") <= 1.0
