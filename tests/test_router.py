"""Router heuristic tests (no API calls)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.router import Tier, heuristic_tier


def test_simple():
    assert heuristic_tier("hello there") is Tier.SIMPLE
    assert heuristic_tier("what is the capital of France") is Tier.SIMPLE
    assert heuristic_tier("define recursion") is Tier.SIMPLE


def test_complex_intent():
    assert heuristic_tier("research AI agents and write a report") is Tier.COMPLEX
    assert heuristic_tier("summarize the latest news on fusion energy") is Tier.COMPLEX


def test_no_topic_leakage():
    # Topic nouns that used to force COMPLEX must not anymore.
    assert heuristic_tier("tell me about mars") is not Tier.COMPLEX
    assert heuristic_tier("what is a rocket") is not Tier.COMPLEX


def test_year_forces_complex():
    assert heuristic_tier("what did the space agency launch in 2024") is Tier.COMPLEX


def test_medium_default():
    text = "Give me an overview of how electric motors work, covering the main parts and their role."
    tier = heuristic_tier(text)
    assert tier in (Tier.MEDIUM, Tier.COMPLEX), tier


if __name__ == "__main__":
    test_simple()
    test_complex_intent()
    test_no_topic_leakage()
    test_year_forces_complex()
    test_medium_default()
    print("[OK] all router tests passed")
