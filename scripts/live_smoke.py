"""Opt-in hosted Jev smoke test using synthetic content only."""

from dbt_jev import JevClassifier


def main() -> None:
    result = JevClassifier.from_env().classify(
        "Synthetic example: a file lookup returned no file during normal exploration.",
        {
            "expected": "An expected miss during exploration",
            "unexpected": "An actual malfunction",
            "unknown": "Insufficient evidence",
        },
    )
    print(result)


if __name__ == "__main__":
    main()
