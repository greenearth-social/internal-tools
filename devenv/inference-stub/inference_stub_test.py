import math

import inference_stub as stub


def _unit(vector):
    return math.sqrt(sum(x * x for x in vector))


class TestPseudoEmbed:
    def test_output_is_a_unit_vector_of_the_expected_width(self):
        # ES cosine-similarity fields reject zero vectors and expect a fixed
        # dimension, so both properties matter downstream.
        out = stub.pseudo_embed([0.1] * 384)
        assert len(out) == stub.OUTPUT_DIM
        assert abs(_unit(out) - 1.0) < 1e-9

    def test_short_input_is_padded(self):
        out = stub.pseudo_embed([1.0, 2.0])
        assert len(out) == stub.OUTPUT_DIM

    def test_zero_vector_does_not_produce_a_zero_output(self):
        out = stub.pseudo_embed([0.0] * 384)
        assert abs(_unit(out) - 1.0) < 1e-9

    def test_is_deterministic(self):
        assert stub.pseudo_embed([0.3] * 384) == stub.pseudo_embed([0.3] * 384)


class TestMeanEmbedding:
    def test_empty_history_still_returns_a_unit_vector(self):
        # A new user hitting a two_tower feed is normal; returning a zero or
        # empty vector would fail the kNN query instead of just being
        # unpersonalized.
        out = stub.mean_embedding([])
        assert len(out) == stub.OUTPUT_DIM
        assert abs(_unit(out) - 1.0) < 1e-9

    def test_single_item_history_matches_that_post(self):
        # Averaging one vector and re-normalizing is a no-op mathematically,
        # but not bit-for-bit, so compare with tolerance.
        vec = [0.5] * 384
        got = stub.mean_embedding([vec])
        want = stub.pseudo_embed(vec)
        assert all(abs(a - b) < 1e-12 for a, b in zip(got, want, strict=True))

    def test_result_is_normalized(self):
        history = [[0.1] * 384, [0.9] * 384, [0.4] * 384]
        assert abs(_unit(stub.mean_embedding(history)) - 1.0) < 1e-9


class TestCosine:
    def test_identical_unit_vectors_score_one(self):
        vec = stub.pseudo_embed([0.7] * 384)
        assert abs(stub.cosine(vec, vec) - 1.0) < 1e-9

    def test_orthogonal_vectors_score_zero(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(stub.cosine(a, b)) < 1e-9

    def test_score_stays_within_the_range_the_api_normalizes_over(self):
        # The api's rank-score handling expects [-1, 1].
        a = stub.pseudo_embed([0.2] * 384)
        b = stub.pseudo_embed([-0.8] * 384)
        assert -1.0 - 1e-9 <= stub.cosine(a, b) <= 1.0 + 1e-9


class TestRankerScoring:
    """The ranker orders candidates by similarity to the user's history."""

    def test_a_candidate_matching_history_outranks_an_unrelated_one(self):
        liked = [1.0] + [0.0] * 383
        unrelated = [0.0, 1.0] + [0.0] * 382
        user = stub.mean_embedding([liked])

        assert stub.cosine(user, stub.pseudo_embed(liked)) > stub.cosine(
            user, stub.pseudo_embed(unrelated)
        )


class TestReadyPayload:
    """The /ready shape is a contract with the api, not a free-form status.

    The api scans `models` for the entry whose "type" is post-tower and reads
    its "model_uuid" — that UUID is stamped on indexed posts and is what the
    two_tower generator filters its kNN query by. Getting the key names wrong
    here doesn't fail loudly at the stub; it surfaces as the two_tower
    generator failing at feed time.
    """

    @staticmethod
    def _handler(path):
        """A Handler with its socket plumbing bypassed.

        Returns the handler and the dict its _send writes into.
        """
        handler = stub.Handler.__new__(stub.Handler)
        handler.path = path
        sent: dict = {}
        handler._send = lambda status, body: sent.update(status=status, body=body)
        return handler, sent

    def _ready(self):
        import json

        handler, sent = self._handler("/ready")
        stub.Handler.do_GET(handler)
        assert sent["status"] == 200
        # Round-trip through JSON: the real client only ever sees serialized form.
        return json.loads(json.dumps(sent["body"]))

    def test_models_entries_are_keyed_on_type(self):
        payload = self._ready()
        for entry in payload["models"]:
            assert isinstance(entry.get("type"), str), entry

    def test_post_tower_entry_carries_a_non_empty_model_uuid(self):
        payload = self._ready()
        post_tower = [m for m in payload["models"] if m["type"] == "post-tower"]
        assert len(post_tower) == 1
        assert isinstance(post_tower[0]["model_uuid"], str)
        assert post_tower[0]["model_uuid"]

    def test_uuid_matches_the_one_stamped_on_predictions(self):
        # Posts are indexed with the UUID from the predict response; the api
        # filters kNN on the UUID from /ready. They have to agree.
        payload = self._ready()
        post_tower = next(m for m in payload["models"] if m["type"] == "post-tower")
        handler, _ = self._handler("/models/post-tower/predict")
        _, predict = stub.Handler._post_tower(handler, {"post_embeddings": [[0.1] * 384]})
        assert post_tower["model_uuid"] == predict["model_uuid"]

    def test_reports_ready(self):
        assert self._ready()["ready"] is True

    def test_advertises_every_endpoint_the_api_calls(self):
        types = {m["type"] for m in self._ready()["models"]}
        assert {"post-tower", "user-tower", "ranker"} <= types


class TestRequestLogging:
    """Docker polls /health forever; those probes must not bury real traffic."""

    @staticmethod
    def _log(path: str, capsys) -> str:
        handler = stub.Handler.__new__(stub.Handler)
        handler.path = path
        stub.Handler.log_message(handler, '"%s" %s -', "GET " + path, "200")
        return capsys.readouterr().out

    def test_health_probes_are_not_logged(self, capsys):
        assert self._log("/health", capsys) == ""

    def test_real_requests_are_still_logged(self, capsys):
        out = self._log("/some/endpoint", capsys)
        assert "inference-stub" in out
        assert "/some/endpoint" in out
