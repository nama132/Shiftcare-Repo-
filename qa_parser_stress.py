"""Phase 3 — adversarial parser stress test.

Exercises ai_parser.parse_message() directly (deterministic pre-classifier +
real Claude fallback) against messy, real-world-style inputs: typos, slang,
multi-sentence, mixed intent, emoji-only, rambling. Each case has an expected
intent; mismatches are reported but do not raise (informational, since some
genuinely ambiguous cases are expected to fall to "other").

Run: python qa_parser_stress.py
"""
import os
os.environ.setdefault("DRY_RUN_SMS", "1")

import ai_parser

CASES = [
    # ---- typos ----
    ("yess", "confirm_coverage"),
    ("yeahh sure thing", "confirm_coverage"),
    ("noo i cant", "decline_coverage"),
    ("cancle my shift im sick", "cancel_shift"),
    ("i cant maek it 2day", "cancel_shift"),
    ("shurr i got it", "confirm_coverage"),

    # ---- slang / casual ----
    ("bet, i got you", "confirm_coverage"),
    ("nah fam cant do it", "decline_coverage"),
    ("hard pass", "decline_coverage"),
    ("say less, on my way", "confirm_coverage"),
    ("lol no", "decline_coverage"),
    ("yeaaa i'm down", "confirm_coverage"),

    # ---- multi-sentence ----
    ("Hey, sorry to bug you. I actually can't make my shift today, I woke up with a fever.", "cancel_shift"),
    ("Got your text. Yes I can cover that shift, what time does it start?", "confirm_coverage"),
    ("Thanks for asking but I already have plans tonight so I can't take this one.", "decline_coverage"),

    # ---- mixed intent (should favor the clearer/actionable signal) ----
    ("I'm sick today so I can't come in, but I could maybe cover tomorrow", "cancel_shift"),
    ("Sorry can't cover this one, but if Maria needs someone I'm calling out too actually", "cancel_shift"),

    # ---- emoji-only ----
    ("👍", "confirm_coverage"),
    ("👎", "decline_coverage"),
    ("😩🤒", "other"),

    # ---- rambling / low-signal ----
    ("ugh today has been such a long day, anyway", "other"),
    ("can you call me instead", "other"),
    ("what time was the shift again", "other"),
    ("who is this", "other"),

    # ---- unambiguous baseline (should never fail) ----
    ("yes", "confirm_coverage"),
    ("no", "decline_coverage"),
    ("I'm calling out sick today", "cancel_shift"),
    ("can't make it in today, car broke down", "cancel_shift"),
]


def main():
    passed, failed = [], []
    for text, expected in CASES:
        result = ai_parser.parse_message(text)
        intent = result.get("intent")
        ok = intent == expected
        (passed if ok else failed).append((text, expected, intent))
        mark = "✅" if ok else "❌"
        print(f"  {mark} {text!r:55s} expected={expected:18s} got={intent}")

    print(f"\nSTRESS RESULT: {len(passed)} passed / {len(failed)} failed (of {len(CASES)})")
    if failed:
        print("\nMismatches:")
        for text, expected, got in failed:
            print(f"  - {text!r}: expected {expected!r}, got {got!r}")


if __name__ == "__main__":
    main()
