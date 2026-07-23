import base64
import json
import time

import mint_token
import pytest


def decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


DID = "did:plc:wpbtckuxxapmfqt3673lxnzx"


def test_token_has_three_jwt_segments():
    assert mint_token.mint(DID).count(".") == 2


def test_token_is_unsigned():
    # The Auth emulator ignores the signature; a real signature would need the
    # service-account key this environment deliberately doesn't have.
    header, _, signature = mint_token.mint(DID).split(".")
    assert decode_segment(header)["alg"] == "none"
    assert signature == ""


def test_uid_is_the_full_did():
    # api's verify_firebase_auth rejects any uid that doesn't start with
    # did:plc:, then strips the prefix for the Firestore document key.
    payload = decode_segment(mint_token.mint(DID).split(".")[1])
    assert payload["uid"] == DID


def test_audience_is_the_identity_toolkit():
    # Firebase rejects a custom token whose aud isn't this exact string.
    payload = decode_segment(mint_token.mint(DID).split(".")[1])
    assert payload["aud"] == mint_token.AUDIENCE


def test_token_is_currently_valid_and_expires():
    payload = decode_segment(mint_token.mint(DID).split(".")[1])
    now = int(time.time())
    assert payload["iat"] <= now + 1
    assert payload["exp"] > now
    # Short-lived on purpose: it's pasted into a URL and lands in shell
    # history and browser history.
    assert payload["exp"] - payload["iat"] <= 3600


def test_segments_are_unpadded_base64url():
    # '=' padding is not valid in a JWT segment and some parsers reject it.
    for segment in mint_token.mint(DID).split(".")[:2]:
        assert "=" not in segment
        assert "+" not in segment and "/" not in segment


def test_rejects_a_non_plc_uid():
    with pytest.raises(SystemExit):
        mint_token.token_for("alice.bsky.social")


def test_accepts_a_plc_uid():
    assert mint_token.token_for(DID).startswith("ey")
